"""
Integration test: MCP protocol flow with mock xiaozhi.me server.
Verifies: initialize → tools/list → tools/call (claude.status).
"""
import asyncio
import json
import pytest
import websockets
from xiaozhi_claude_mcp.protocol import (
    encode_envelope,
    encode_jsonrpc_request,
)


@pytest.mark.asyncio
async def test_mcp_initialize_and_list_tools():
    async def mock_xiaozhi(ws):
        async for raw_msg in ws:
            msg = json.loads(raw_msg)
            payload = msg["payload"]

            if payload.get("method") == "initialize":
                await ws.send(json.dumps({
                    "session_id": "mock_sess",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "mock", "version": "1.0"},
                        },
                    },
                }))

            elif payload.get("method") == "tools/list":
                await ws.send(json.dumps({
                    "session_id": "mock_sess",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "claude.status",
                                    "description": "Get Claude Code state",
                                    "inputSchema": {"type": "object", "properties": {}},
                                },
                            ],
                        },
                    },
                }))

            elif payload.get("method") == "tools/call":
                tool_name = payload["params"]["name"]
                if tool_name == "claude.status":
                    await ws.send(json.dumps({
                        "session_id": "mock_sess",
                        "type": "mcp",
                        "payload": {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "result": {
                                "content": [{
                                    "type": "text",
                                    "text": json.dumps({
                                        "total": 1,
                                        "running": 0,
                                        "waiting": 0,
                                        "msg": "",
                                        "entries": [],
                                        "tokens": 0,
                                        "tokens_today": 0,
                                    }),
                                }],
                            },
                        },
                    }))

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]

    async with websockets.connect(f"ws://localhost:{port}") as ws:
        # initialize
        req = encode_jsonrpc_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        }, id=1)
        env = encode_envelope("test_sess", req)
        await ws.send(json.dumps(env))
        resp = json.loads(await ws.recv())
        assert resp["payload"]["result"]["serverInfo"]["name"] == "mock"

        # tools/list
        req2 = encode_jsonrpc_request("tools/list", {}, id=2)
        env2 = encode_envelope("test_sess", req2)
        await ws.send(json.dumps(env2))
        resp2 = json.loads(await ws.recv())
        tools = resp2["payload"]["result"]["tools"]
        assert any(t["name"] == "claude.status" for t in tools)

        # tools/call claude.status
        req3 = encode_jsonrpc_request("tools/call", {
            "name": "claude.status",
            "arguments": {},
        }, id=3)
        env3 = encode_envelope("test_sess", req3)
        await ws.send(json.dumps(env3))
        resp3 = json.loads(await ws.recv())
        content = resp3["payload"]["result"]["content"][0]["text"]
        status = json.loads(content)
        assert "total" in status
        assert "waiting" in status

    server.close()
    await server.wait_closed()
