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
import time
import uuid

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
from xiaozhi_claude_mcp.terminal_auth import TerminalAuth
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


def _request_exists(perm_dir: str, permission_id: str) -> bool:
    return os.path.exists(os.path.join(perm_dir, f"{permission_id}.json"))


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
        cfg = self.config.server
        self._terminal_auth = TerminalAuth(
            password=cfg.terminal_password,
            enable_voice=cfg.enable_voice_auth,
            enable_display=cfg.enable_display_auth,
        ) if (cfg.terminal_password or cfg.enable_voice_auth
              or cfg.enable_display_auth) else None

        # Dev mode: auto-generate token if no auth configured
        terminal_token = cfg.terminal_token
        if not self._terminal_auth and not terminal_token and cfg.show_terminal:
            if cfg.env == "dev":
                import secrets
                terminal_token = secrets.token_hex(8)
                logger.warning("Dev mode: auto-generated terminal token: %s "
                             "(use ?token=%s)", terminal_token, terminal_token)
            else:
                logger.warning("Prod mode: terminal is open with NO authentication! "
                             "Set terminal_password, enable_voice_auth, "
                             "enable_display_auth, or terminal_token.")

        self._hook_server = HookServer(
            host=self.config.server.hook_host,
            port=self.config.server.hook_port,
            show_terminal=self.config.server.show_terminal,
            allow_terminal_input=self.config.server.allow_terminal_input,
            terminal_token=terminal_token,
            allow_token=(cfg.env == "dev"),
            terminal_auth=self._terminal_auth,
        )
        self._running = False
        self._pending_perm_check_task: asyncio.Task | None = None
        self._pending_tool_tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._send_future: asyncio.Future | None = None
        self._task_results: dict[str, dict] = {}
        self._task_lock = asyncio.Lock()

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
            if self._pty and self._pty.alive:
                d["pty"] = self._pty.get_status()
            async with self._task_lock:
                pending_tasks = {tid: t for tid, t in self._task_results.items()
                                 if t["status"] == "pending"}
                done_tasks = {tid: t for tid, t in self._task_results.items()
                              if t["status"] == "done" and not t.get("consumed")}
                d["pending_tasks"] = len(pending_tasks)
                d["completed_task_ids"] = list(done_tasks.keys())[:5]
            # Include pending permission details
            if pending:
                d["permission_requests"] = [
                    {"permission_id": r.permission_id, "tool": r.tool, "hint": r.hint}
                    for r in pending[:10]
                ]
            return d

        elif name == "claude.send_message":
            prompt = args["prompt"]
            session_id = args.get("session_id", "")
            max_turns = args.get("max_turns", 2)
            if not isinstance(max_turns, int) or max_turns < 1:
                max_turns = 2
            if max_turns > 10:
                max_turns = 10

            # Reset session binding so next PTY hook establishes correct session
            if self._pty:
                self._pty.session_id = ""
            task_id = str(uuid.uuid4())[:8]
            async with self._task_lock:
                self._task_results[task_id] = {
                    "status": "pending",
                    "prompt": prompt,
                    "session_id": session_id,
                    "max_turns": max_turns,
                    "created_at": time.time(),
                }
            # Start background Claude task
            asyncio.create_task(self._run_async_send_message(task_id))
            logger.info("send_message: async task %s started, prompt=%s",
                        task_id, prompt[:80])
            return {
                "status": "processing",
                "task_id": task_id,
                "message": f"正在让Claude处理你的问题（任务{task_id}），稍后调用 claude.get_result 获取结果",
            }

        elif name == "claude.get_result":
            task_id = args["task_id"]
            async with self._task_lock:
                task = self._task_results.get(task_id)
                if not task:
                    return {"status": "not_found", "message": "任务不存在或已过期"}
                if task["status"] == "pending":
                    preview = ""
                    if self._pty and self._pty.alive:
                        cleaned = self._pty.get_recent_output(raw=False)
                        preview = cleaned[-300:] if cleaned else ""
                    return {
                        "status": "pending",
                        "message": "任务还在处理中，稍后再查",
                        "preview": preview,
                    }
                if task["status"] == "error":
                    return {"status": "error", "message": task.get("error", "未知错误")}
                # Done — mark as consumed so it doesn't clutter status
                task["consumed"] = True
                result = {
                    "status": "done",
                    "content": task["content"],
                    "session_id": task.get("session_id", ""),
                    "task_id": task_id,
                }
            # Send claude_turn notification with the result
            try:
                await self.transport.send_notification(
                    "notifications/claude_turn",
                    {
                        "role": "assistant",
                        "content": task["content"],
                        "session_id": task.get("session_id", ""),
                        "tokens": 0,
                    },
                )
            except Exception:
                logger.warning("Failed to send claude_turn notification", exc_info=True)
            return result

        elif name == "claude.approve":
            perm_id = args["permission_id"]
            if not _request_exists(self.config.claude.perm_dir, perm_id):
                return {"ok": False, "error": f"权限请求不存在或已处理: {perm_id}"}
            write_permission_result(self.config.claude.perm_dir, perm_id, True)
            cleanup_request(self.config.claude.perm_dir, perm_id, keep_result=True)
            asyncio.create_task(self._auto_answer_pty(b"1\r"))
            return {"ok": True}

        elif name == "claude.deny":
            perm_id = args["permission_id"]
            if not _request_exists(self.config.claude.perm_dir, perm_id):
                return {"ok": False, "error": f"权限请求不存在或已处理: {perm_id}"}
            write_permission_result(self.config.claude.perm_dir, perm_id, False)
            cleanup_request(self.config.claude.perm_dir, perm_id, keep_result=True)
            asyncio.create_task(self._auto_answer_pty(b"3\r"))
            return {"ok": True}

        elif name == "claude.prepare_voice_login":
            if not self._terminal_auth or not self._terminal_auth.has_voice:
                return {"ok": False, "error": "语音验证未启用"}
            return {
                "ok": True,
                "message": (
                    "好的，请念出网页上显示的6位字母数字验证码。"
                    "验证码60秒有效，过期后需刷新网页重新生成。"
                ),
                "expire_seconds": 60,
            }

        elif name == "claude.voice_approve_login":
            if not self._terminal_auth:
                return {"ok": False, "error": "终端验证未启用"}
            spoken_code = args.get("code", "")
            challenge_id, error = self._terminal_auth.approve_voice(spoken_code)
            if challenge_id:
                return {"ok": True, "message": "终端语音验证已批准"}
            if error == "expired":
                return {"ok": False, "error": (
                    "验证码已过期（60秒有效）。请让用户刷新网页重新生成验证码。"
                )}
            return {"ok": False, "error": (
                "验证码错误，请确认用户念的字母和数字是否正确，再试一次。"
            )}

        elif name == "claude.get_login_code":
            if not self._terminal_auth:
                return {"ok": False, "error": "终端验证未启用"}
            result = self._terminal_auth.request_display_challenge()
            if result:
                return result
            return {"ok": False, "error": "无法生成验证码"}

        elif name in ("claude.notify_permission", "claude.notify_turn"):
            return {"ok": True}

        else:
            raise ValueError(f"Unknown tool: {name}")

    # ── send_message via PTY ───────────────────────────────────

    async def _auto_answer_pty(self, keys: bytes, delay: float = 0.5) -> None:
        """Send keystroke to PTY after a delay, for auto-answering permission dialogs."""
        await asyncio.sleep(delay)
        if self._pty and self._pty.alive:
            self._pty.write_raw(keys)
            logger.info("Auto-answered PTY dialog with keys=%r", keys)

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

    async def _run_async_send_message(self, task_id: str) -> None:
        """Background task: run Claude and store result."""
        async with self._task_lock:
            task = self._task_results.get(task_id)
            if not task:
                return
        prompt = task["prompt"]
        session_id = task.get("session_id", "")
        max_turns = task.get("max_turns", 2)

        try:
            result = await self._do_send_message(prompt, session_id, max_turns)
            async with self._task_lock:
                if task_id in self._task_results:
                    self._task_results[task_id].update(
                        status="done",
                        content=result["content"],
                        session_id=result.get("session_id", ""),
                    )
            logger.info("Async task %s completed", task_id)
        except Exception as e:
            async with self._task_lock:
                if task_id in self._task_results:
                    self._task_results[task_id].update(
                        status="error",
                        error=str(e),
                    )
            logger.error("Async task %s failed: %s", task_id, e)

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
