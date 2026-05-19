"""
Integration test: MCP protocol flow with mock xiaozhi.me server.
Verifies: initialize → tools/list → tools/call (claude.status).
Uses raw JSON-RPC 2.0 (no envelope).
"""
import json
import pytest
import websockets
import pytest_asyncio


@pytest.mark.asyncio
async def test_mcp_initialize_and_list_tools():
    async def mock_xiaozhi(ws):
        async for raw_msg in ws:
            msg = json.loads(raw_msg)

            if msg.get("method") == "initialize":
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock", "version": "1.0"},
                    },
                }))

            elif msg.get("method") == "tools/list":
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "claude.status",
                                "description": "Get Claude Code state",
                                "inputSchema": {"type": "object", "properties": {}},
                            },
                        ],
                    },
                }))

            elif msg.get("method") == "tools/call":
                tool_name = msg["params"]["name"]
                if tool_name == "claude.status":
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
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
                    }))

    server = await websockets.serve(mock_xiaozhi, "localhost", 0)
    port = server.sockets[0].getsockname()[1]

    async with websockets.connect(f"ws://localhost:{port}") as ws:
        # initialize
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }))
        resp = json.loads(await ws.recv())
        assert resp["result"]["serverInfo"]["name"] == "mock"

        # tools/list
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 2,
            "params": {},
        }))
        resp2 = json.loads(await ws.recv())
        tools = resp2["result"]["tools"]
        assert any(t["name"] == "claude.status" for t in tools)

        # tools/call claude.status
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 3,
            "params": {"name": "claude.status", "arguments": {}},
        }))
        resp3 = json.loads(await ws.recv())
        content = resp3["result"]["content"][0]["text"]
        status = json.loads(content)
        assert "total" in status
        assert "waiting" in status

    server.close()
    await server.wait_closed()
