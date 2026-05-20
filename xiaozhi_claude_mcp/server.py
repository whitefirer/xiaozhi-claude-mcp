"""
MCP Server connecting Claude Code to xiaozhi.me.

Uses a persistent PTY Claude session (no claude -p cold starts).
Entry point: python -m xiaozhi_claude_mcp.server config.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from xiaozhi_claude_mcp.config import load_config
from xiaozhi_claude_mcp.protocol import (
    parse_jsonrpc,
    JsonRpcRequest,
    JsonRpcNotification,
)
from xiaozhi_claude_mcp.transport import XiaozhiTransport, TransportState
from xiaozhi_claude_mcp.mcp_tools import get_tools_list, make_text_content
from xiaozhi_claude_mcp.pty_session import AsyncPTYSession
from xiaozhi_claude_mcp.hook_server import HookServer
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
        self.status_monitor = StatusMonitor(
            exclude_paths=self.config.status.exclude_paths,
            exclude_kinds=self.config.status.exclude_kinds,
        )
        self._pty: AsyncPTYSession | None = None
        self._hook_server = HookServer()
        self._running = False
        self._pending_perm_check_task: asyncio.Task | None = None
        self._pending_tool_tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._send_future: asyncio.Future | None = None

    async def run(self) -> None:
        self._running = True

        # Start hook server (receives Stop hook callbacks) + web terminal
        await self._hook_server.start()

        while self._running:
            try:
                # Start persistent PTY Claude session
                if self._pty is None or not self._pty.alive:
                    self._pty = AsyncPTYSession(cwd=os.getcwd())
                    await self._pty.start()
                    self._hook_server.set_pty_session(self._pty)

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

    async def shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        try:
            if self.transport.state == TransportState.CONNECTED:
                await self.transport.disconnect()
        except Exception:
            pass
        if self._pty:
            await self._pty.stop()
        await self._hook_server.stop()

    # ── session handler ────────────────────────────────────────

    async def _handle_session(self) -> None:
        self._pending_tool_tasks.clear()
        while self._running and self.transport.state == TransportState.CONNECTED:
            raw = await self.transport.recv()
            parsed = parse_jsonrpc(raw)

            if isinstance(parsed, JsonRpcNotification):
                logger.debug("Notification: %s", parsed.method)
                continue

            if isinstance(parsed, JsonRpcRequest):
                if parsed.method == "ping":
                    await self.transport.send_response(parsed.id, {})
                elif parsed.method == "tools/call":
                    task = asyncio.create_task(self._handle_request(parsed))
                    self._pending_tool_tasks.add(task)
                    task.add_done_callback(self._pending_tool_tasks.discard)
                else:
                    await self._handle_request(parsed)

        for task in list(self._pending_tool_tasks):
            task.cancel()

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
                try:
                    await self.transport.send_error(req.id, -32000, str(e))
                except Exception:
                    pass

        elif method == "ping":
            await self.transport.send_response(req.id, {})

        else:
            await self.transport.send_error(req.id, -32601, f"Unknown method: {method}")

    # ── tool dispatch ──────────────────────────────────────────

    async def _call_tool(self, name: str, args: dict) -> dict:
        if name == "claude.status":
            pending = scan_for_requests(self.config.claude.perm_dir)
            hb = self.status_monitor.get_heartbeat(pending)
            d = hb.to_dict()
            # Add PTY state so LLM can see if Claude is alive
            if self._pty and self._pty.alive:
                d["pty"] = self._pty.get_status()
            return d

        elif name == "claude.send_message":
            prompt = args["prompt"]
            session_id = args.get("session_id", "")
            max_turns = args.get("max_turns", 2)
            if not isinstance(max_turns, int) or max_turns < 1:
                max_turns = 2
            if max_turns > 10:
                max_turns = 10

            async with self._send_lock:
                if self._send_future is not None and not self._send_future.done():
                    logger.info("send_message: piggybacking on in-flight request")
                    future = self._send_future
                else:
                    future = asyncio.ensure_future(
                        self._do_send_message(prompt, session_id, max_turns)
                    )
                    self._send_future = future

            result = await future
            try:
                await self.transport.send_notification(
                    "notifications/claude_turn",
                    {
                        "role": "assistant",
                        "content": result["content"],
                        "session_id": result.get("session_id", ""),
                        "tokens": result.get("tokens", 0),
                    },
                )
            except Exception:
                logger.warning("Failed to send claude_turn notification", exc_info=True)
            return result

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

    # ── send_message via PTY ───────────────────────────────────

    async def _do_send_message(self, prompt: str, session_id: str,
                               max_turns: int = 2) -> dict:
        """Send prompt via PTY to persistent Claude, wait for Stop hook."""
        if self._pty is None or not self._pty.alive:
            raise RuntimeError("PTY session not available")

        # Get real PTY session_id (discovered from Stop hooks)
        pty_sid = self._pty.session_id

        # Build prompt with session context
        if pty_sid:
            full_prompt = f"[会话 {pty_sid[:8]}, 最多{max_turns}轮] {prompt}"
        else:
            full_prompt = f"[最多{max_turns}轮] {prompt}"

        # Send to PTY and wait for turn complete
        timeout = max(30, max_turns * 30)  # 30s per turn
        output = await self._pty.send_prompt(full_prompt, timeout=timeout)

        return {
            "content": output,
            "session_id": pty_sid,
            "tokens": 0,
            "cost_usd": 0,
        }

    # ── permission polling ─────────────────────────────────────

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


# ── entry point ──────────────────────────────────────────────────

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


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
