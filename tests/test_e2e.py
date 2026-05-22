"""
End-to-end test: full MCP server pipeline with mock xiaozhi and mock PTY.

Verifies:
- initialize → tools/list → claude.status
- Async claude.send_message → claude.get_result
- claude.approve sends 1\\r to PTY, claude.deny sends 3\\r to PTY
- get_result on non-existent task returns not_found
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import websockets
import yaml


SAMPLE_CONFIG = {
    "server": {
        "xiaozhi_endpoint": "ws://placeholder",
        "reconnect_interval": 1,
    },
    "claude": {
        "perm_dir": "/tmp/claude-xiaozhi-perms-test",
    },
    "status": {
        "poll_interval_sec": 5,
        "exclude_paths": [],
        "exclude_kinds": [],
    },
}


def _make_config(endpoint: str) -> str:
    cfg = dict(SAMPLE_CONFIG)
    cfg["server"]["xiaozhi_endpoint"] = endpoint
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(cfg, f)
        return f.name


class MockPTY:
    """Mock AsyncPTYSession that stays 'alive' and returns canned responses."""

    def __init__(self, *args, **kwargs):
        self.state = MagicMock()
        self.state.name = "IDLE"
        self._pid = 99999
        self.session_id = "mock-sid-abc123"
        self._output_callback = None
        self._turn_event = asyncio.Event()
        self._turn_event.set()
        self.written_keys: list[bytes] = []

    @property
    def alive(self):
        return True

    @property
    def pid(self):
        return self._pid

    async def start(self):
        pass

    async def stop(self):
        pass

    def set_output_callback(self, cb):
        self._output_callback = cb

    async def send_prompt(self, prompt, timeout=180):
        await asyncio.sleep(0.2)
        return f"[Mock Claude response to: {prompt[:50]}...]"

    def notify_turn_complete(self, output_hint=""):
        pass

    def get_status(self):
        return {"state": "idle", "pid": self._pid, "session_id": self.session_id}

    def get_recent_output(self, raw=False):
        return "[mock output]"

    def get_recent_output_bytes(self):
        return b"[mock output]"

    def write_raw(self, data):
        self.written_keys.append(data)


async def _recv_until_id(ws, expected_id, notifications, timeout=3.0):
    """Read messages until we get one with the expected id. Collect notifications."""
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "result" in msg and msg.get("id") == expected_id:
            return msg
        if "error" in msg and msg.get("id") == expected_id:
            return msg
        notifications.append(msg)


@pytest.mark.asyncio
async def test_e2e_async_send_message_flow():
    """Async send_message → get_result flow."""
    responses = {}
    notifications = []

    async def mock_xiaozhi(ws):
        # initialize
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "xiaozhi", "version": "1.0"}},
        }))
        resp = await _recv_until_id(ws, 1, notifications)
        responses[1] = resp

        # tools/list
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {},
        }))
        resp = await _recv_until_id(ws, 2, notifications)
        responses[2] = resp
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert "claude.get_result" in tool_names

        # Async send_message — returns immediately
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {"name": "claude.send_message",
                       "arguments": {"prompt": "Hello Claude"}},
        }))
        resp = await _recv_until_id(ws, 3, notifications)
        responses[3] = resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["status"] == "processing"
        assert "task_id" in result
        task_id = result["task_id"]

        # claude.status — should show pending task
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 4,
            "params": {"name": "claude.status", "arguments": {}},
        }))
        resp = await _recv_until_id(ws, 4, notifications)
        responses[4] = resp
        status = json.loads(resp["result"]["content"][0]["text"])
        assert status["pending_tasks"] >= 1

        # Wait for background task to complete
        await asyncio.sleep(0.5)

        # claude.get_result
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 5,
            "params": {"name": "claude.get_result",
                       "arguments": {"task_id": task_id}},
        }))
        resp = await _recv_until_id(ws, 5, notifications)
        responses[5] = resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["status"] == "done"
        assert "Mock Claude response" in result["content"]

        # Drain notifications
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3))
                notifications.append(msg)
        except asyncio.TimeoutError:
            pass

        # ping
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "ping", "id": 6, "params": {},
        }))
        resp = await _recv_until_id(ws, 6, notifications)
        responses[6] = resp

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    config_path = _make_config(f"ws://localhost:{port}")

    from xiaozhi_claude_mcp.server import XiaozhiClaudeMCPServer

    with patch("xiaozhi_claude_mcp.server.AsyncPTYSession", MockPTY):
        mcp = XiaozhiClaudeMCPServer(config_path)
        task = asyncio.create_task(mcp.run())
        await asyncio.sleep(1.0)

        assert 1 in responses, "Missing initialize"
        assert 2 in responses, "Missing tools/list"
        assert 3 in responses, "Missing send_message"
        assert 4 in responses, "Missing claude.status"
        assert 5 in responses, "Missing get_result"
        assert 6 in responses, "Missing ping"

        # Verify claude_turn notification sent after get_result
        turn_notifs = [n for n in notifications
                       if n.get("method") == "notifications/claude_turn"]
        assert len(turn_notifs) >= 1, f"Expected claude_turn notification"

        await mcp.shutdown()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    server.close()
    await server.wait_closed()
    os.unlink(config_path)


@pytest.mark.asyncio
async def test_e2e_get_result_pending():
    """get_result on a still-pending task returns 'pending'."""
    notifications = []

    async def mock_xiaozhi(ws):
        # initialize
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        }))
        await _recv_until_id(ws, 1, notifications)

        # tools/list
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {},
        }))
        await _recv_until_id(ws, 2, notifications)

        # send_message
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {"name": "claude.send_message",
                       "arguments": {"prompt": "test"}},
        }))
        resp = await _recv_until_id(ws, 3, notifications)
        result = json.loads(resp["result"]["content"][0]["text"])
        task_id = result["task_id"]

        # get_result with non-existent task
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 4,
            "params": {"name": "claude.get_result",
                       "arguments": {"task_id": "nonexist"}},
        }))
        resp = await _recv_until_id(ws, 4, notifications)
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["status"] == "not_found"

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    config_path = _make_config(f"ws://localhost:{port}")

    from xiaozhi_claude_mcp.server import XiaozhiClaudeMCPServer

    with patch("xiaozhi_claude_mcp.server.AsyncPTYSession", MockPTY):
        mcp = XiaozhiClaudeMCPServer(config_path)
        task = asyncio.create_task(mcp.run())
        await asyncio.sleep(1.0)

        await mcp.shutdown()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    server.close()
    await server.wait_closed()
    os.unlink(config_path)


@pytest.mark.asyncio
async def test_e2e_permission_approve_deny():
    """Approve sends 1\\r to PTY, deny sends 3\\r, and both return ok."""
    notifications = []
    perm_dir = "/tmp/claude-xiaozhi-perms-test"
    os.makedirs(perm_dir, exist_ok=True)

    # Simulate PermissionRequest hook output — write request files
    from xiaozhi_claude_mcp.permission_broker import write_permission_request

    approve_id = "test-approve-bash-1"
    deny_id = "test-deny-write-2"
    write_permission_request(perm_dir, approve_id, "Bash", "rm /tmp/foo")
    write_permission_request(perm_dir, deny_id, "Write", "/tmp/bar")

    async def mock_xiaozhi(ws):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        }))
        await _recv_until_id(ws, 1, notifications)

        # claude.approve
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {"name": "claude.approve",
                       "arguments": {"permission_id": approve_id}},
        }))
        resp = await _recv_until_id(ws, 2, notifications)
        assert json.loads(resp["result"]["content"][0]["text"]) == {"ok": True}

        # claude.deny
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {"name": "claude.deny",
                       "arguments": {"permission_id": deny_id}},
        }))
        resp = await _recv_until_id(ws, 3, notifications)
        assert json.loads(resp["result"]["content"][0]["text"]) == {"ok": True}

        # Cleanup
        for f in os.listdir(perm_dir):
            os.unlink(os.path.join(perm_dir, f))

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    config_path = _make_config(f"ws://localhost:{port}")

    from xiaozhi_claude_mcp.server import XiaozhiClaudeMCPServer

    with patch("xiaozhi_claude_mcp.server.AsyncPTYSession", MockPTY) as mock_cls:
        mcp = XiaozhiClaudeMCPServer(config_path)
        task = asyncio.create_task(mcp.run())
        await asyncio.sleep(1.0)

        pty_instance = mcp._pty

        # Wait for async auto-answer tasks to complete (0.5s delay + buffer)
        await asyncio.sleep(0.8)

        # Verify PTY keystrokes
        assert b"1\r" in pty_instance.written_keys, \
            f"Expected approve keystroke 1\\r, got {pty_instance.written_keys}"
        assert b"3\r" in pty_instance.written_keys, \
            f"Expected deny keystroke 3\\r, got {pty_instance.written_keys}"

        await mcp.shutdown()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    server.close()
    await server.wait_closed()
    os.unlink(config_path)
