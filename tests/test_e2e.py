"""
End-to-end test: full MCP server pipeline with mock xiaozhi and mock PTY.

Verifies: initialize → tools/list → claude.status → claude.send_message
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
        pass


async def _recv_until_id(ws, expected_id: int, notifications: list, timeout: float = 2.0):
    """Read messages until we get one with the expected id. Collect notifications."""
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "result" in msg and msg.get("id") == expected_id:
            return msg
        if "error" in msg and msg.get("id") == expected_id:
            return msg
        # Notification or response for different id — collect it
        notifications.append(msg)


@pytest.mark.asyncio
async def test_e2e_mcp_protocol_flow():
    """Full MCP protocol: initialize, tools/list, claude.status, claude.send_message."""
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
        assert resp["result"]["serverInfo"]["name"] == "xiaozhi-claude-mcp"

        # tools/list
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {},
        }))
        resp = await _recv_until_id(ws, 2, notifications)
        responses[2] = resp
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert "claude.status" in tool_names
        assert "claude.send_message" in tool_names

        # claude.status
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {"name": "claude.status", "arguments": {}},
        }))
        resp = await _recv_until_id(ws, 3, notifications)
        responses[3] = resp
        status = json.loads(resp["result"]["content"][0]["text"])
        assert "pty" in status

        # claude.send_message (notification sent before response)
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 4,
            "params": {"name": "claude.send_message",
                       "arguments": {"prompt": "Hello", "max_turns": 1}},
        }))
        resp = await _recv_until_id(ws, 4, notifications)
        responses[4] = resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert "Mock Claude response" in result["content"]

        # Drain remaining notifications
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3))
                notifications.append(msg)
        except asyncio.TimeoutError:
            pass

        # ping
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "ping", "id": 5, "params": {},
        }))
        resp = await _recv_until_id(ws, 5, notifications)
        responses[5] = resp

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    config_path = _make_config(f"ws://localhost:{port}")

    from xiaozhi_claude_mcp.server import XiaozhiClaudeMCPServer

    with patch("xiaozhi_claude_mcp.server.AsyncPTYSession", MockPTY):
        mcp = XiaozhiClaudeMCPServer(config_path)
        task = asyncio.create_task(mcp.run())
        await asyncio.sleep(1.0)

        # Verify all 5 responses received
        assert 1 in responses, "Missing initialize response"
        assert 2 in responses, "Missing tools/list response"
        assert 3 in responses, "Missing claude.status response"
        assert 4 in responses, "Missing claude.send_message response"
        assert 5 in responses, "Missing ping response"

        # Verify claude_turn notification
        turn_notifs = [n for n in notifications
                       if n.get("method") == "notifications/claude_turn"]
        assert len(turn_notifs) == 1, f"Expected 1 claude_turn notification"

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
    """Test claude.approve and claude.deny tool calls."""
    received = []
    notifications = []

    async def mock_xiaozhi(ws):
        # Handshake
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
                       "arguments": {"permission_id": "test-123"}},
        }))
        resp = await _recv_until_id(ws, 2, notifications)
        received.append(resp)
        assert json.loads(resp["result"]["content"][0]["text"]) == {"ok": True}

        # claude.deny
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {"name": "claude.deny",
                       "arguments": {"permission_id": "test-456"}},
        }))
        resp = await _recv_until_id(ws, 3, notifications)
        received.append(resp)
        assert json.loads(resp["result"]["content"][0]["text"]) == {"ok": True}

        # Cleanup permission files
        perm_dir = "/tmp/claude-xiaozhi-perms-test"
        for f in os.listdir(perm_dir):
            if "test-123" in f or "test-456" in f:
                os.unlink(os.path.join(perm_dir, f))

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    config_path = _make_config(f"ws://localhost:{port}")
    os.makedirs("/tmp/claude-xiaozhi-perms-test", exist_ok=True)

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

    assert len(received) == 2
