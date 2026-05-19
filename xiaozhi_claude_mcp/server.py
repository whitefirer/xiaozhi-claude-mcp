"""
MCP Server connecting Claude Code to xiaozhi.me.

Entry point: python -m xiaozhi_claude_mcp.server config.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from xiaozhi_claude_mcp.config import load_config
from xiaozhi_claude_mcp.protocol import (
    parse_jsonrpc,
    JsonRpcRequest,
    JsonRpcNotification,
)
from xiaozhi_claude_mcp.transport import XiaozhiTransport, TransportState
from xiaozhi_claude_mcp.mcp_tools import get_tools_list, make_text_content
from xiaozhi_claude_mcp.claude_driver import ClaudeDriver
from xiaozhi_claude_mcp.permission_broker import (
    scan_for_requests,
    write_permission_result,
    cleanup_request,
)
from xiaozhi_claude_mcp.status_monitor import StatusMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("server")


class XiaozhiClaudeMCPServer:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.transport = XiaozhiTransport(
            self.config.server.xiaozhi_endpoint,
            self.config.server.reconnect_interval,
        )
        self.claude = ClaudeDriver(binary=self.config.claude.binary)
        self.status_monitor = StatusMonitor(
            exclude_paths=self.config.status.exclude_paths,
            exclude_kinds=self.config.status.exclude_kinds,
        )
        self._running = False
        self._pending_perm_check_task: asyncio.Task | None = None

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.transport.connect()
                self._pending_perm_check_task = asyncio.create_task(
                    self._poll_permission_requests()
                )
                await self._handle_session()
            except ConnectionError:
                logger.warning(
                    "Connection failed, retry in %ds",
                    self.config.server.reconnect_interval,
                )
                await asyncio.sleep(self.config.server.reconnect_interval)
            except Exception as e:
                logger.error("Unexpected error: %s", e)
                await asyncio.sleep(self.config.server.reconnect_interval)
            finally:
                if self._pending_perm_check_task:
                    self._pending_perm_check_task.cancel()
                    self._pending_perm_check_task = None
                if self.transport.state == TransportState.CONNECTED:
                    await self.transport.disconnect()

    async def _handle_session(self) -> None:
        while self._running and self.transport.state == TransportState.CONNECTED:
            raw = await self.transport.recv()
            parsed = parse_jsonrpc(raw)

            if isinstance(parsed, JsonRpcNotification):
                logger.debug("Notification: %s", parsed.method)
                continue

            if isinstance(parsed, JsonRpcRequest):
                await self._handle_request(parsed)

    async def _handle_request(self, req: JsonRpcRequest) -> None:
        method = req.method
        logger.info("Handling: %s (id=%s)", method, req.id)

        if method == "initialize":
            await self.transport.send_response(req.id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "xiaozhi-claude-mcp",
                    "version": "0.1.0",
                },
            })

        elif method == "tools/list":
            await self.transport.send_response(req.id, {
                "tools": get_tools_list(),
            })

        elif method == "tools/call":
            tool_name = req.params.get("name", "")
            arguments = req.params.get("arguments", {})
            logger.info("tools/call: %s args=%s", tool_name, arguments)

            try:
                result = await self._call_tool(tool_name, arguments)
                logger.info("tools/call result: %s -> %s", tool_name,
                            json.dumps(result, ensure_ascii=False)[:200])
                await self.transport.send_response(req.id, {
                    "content": make_text_content(json.dumps(result)),
                })
            except Exception as e:
                logger.error("Tool %s failed: %s", tool_name, e)
                await self.transport.send_error(req.id, -32000, str(e))

        elif method == "ping":
            await self.transport.send_response(req.id, {})

        else:
            await self.transport.send_error(req.id, -32601, f"Unknown method: {method}")

    async def _call_tool(self, name: str, args: dict) -> dict:
        if name == "claude.status":
            pending = scan_for_requests(self.config.claude.perm_dir)
            hb = self.status_monitor.get_heartbeat(pending)
            return hb.to_dict()

        elif name == "claude.send_message":
            prompt = args["prompt"]
            session_id = args.get("session_id")
            # Validate session_id — must look like a UUID
            if session_id and not _looks_like_uuid(session_id):
                logger.warning("Invalid session_id '%s', starting new session", session_id)
                session_id = None
            resp = await self.claude.send(prompt, session_id=session_id)
            await self.transport.send_notification(
                "notifications/claude_turn",
                {
                    "role": "assistant",
                    "content": resp.content,
                    "session_id": resp.session_id,
                    "tokens": resp.tokens,
                },
            )
            return {
                "content": resp.content,
                "session_id": resp.session_id,
                "tokens": resp.tokens,
                "cost_usd": resp.cost_usd,
            }

        elif name == "claude.approve":
            perm_id = args["permission_id"]
            write_permission_result(self.config.claude.perm_dir, perm_id, True)
            cleanup_request(self.config.claude.perm_dir, perm_id)
            return {"ok": True}

        elif name == "claude.deny":
            perm_id = args["permission_id"]
            write_permission_result(self.config.claude.perm_dir, perm_id, False)
            cleanup_request(self.config.claude.perm_dir, perm_id)
            return {"ok": True}

        elif name in ("claude.notify_permission", "claude.notify_turn"):
            return {"ok": True}

        else:
            raise ValueError(f"Unknown tool: {name}")

    async def _poll_permission_requests(self) -> None:
        while self._running:
            try:
                requests = scan_for_requests(self.config.claude.perm_dir)
                for req in requests:
                    await self.transport.send_notification(
                        "notifications/claude_notify_permission",
                        {
                            "permission_id": req.permission_id,
                            "tool": req.tool,
                            "hint": req.hint,
                        },
                    )
            except Exception as e:
                logger.error("Permission poll error: %s", e)
            await asyncio.sleep(self.config.status.poll_interval_sec)

    async def shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        try:
            if self.transport.state == TransportState.CONNECTED:
                await self.transport.disconnect()
        except Exception:
            pass


async def _main():
    if len(sys.argv) < 2:
        print("Usage: python -m xiaozhi_claude_mcp.server <config.yaml>")
        sys.exit(1)

    server = XiaozhiClaudeMCPServer(sys.argv[1])
    try:
        await server.run()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        await server.shutdown()


def _looks_like_uuid(s: str) -> bool:
    """Quick check: UUIDs are 36 chars with dashes like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx."""
    return len(s) >= 32 and "-" in s


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
