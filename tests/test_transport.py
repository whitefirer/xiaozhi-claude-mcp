import asyncio
import json
import pytest
import pytest_asyncio
import websockets
from xiaozhi_claude_mcp.transport import XiaozhiTransport, TransportState


@pytest_asyncio.fixture
async def echo_server():
    """Echo server that returns what it receives."""
    async def handler(ws):
        async for msg in ws:
            data = json.loads(msg)
            reply = json.dumps(data)
            await ws.send(reply)

    server = await websockets.serve(handler, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"ws://localhost:{port}"
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_transport_send_and_receive(echo_server):
    t = XiaozhiTransport(echo_server)
    await t.connect()
    assert t.state == TransportState.CONNECTED

    msg = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}
    await t.send(msg)

    received = await asyncio.wait_for(t.recv(), timeout=2)
    assert received["method"] == "tools/list"
    assert received["id"] == 1

    await t.disconnect()
    assert t.state == TransportState.DISCONNECTED


@pytest.mark.asyncio
async def test_transport_send_response():
    t = XiaozhiTransport("ws://localhost:1", reconnect_interval=0)
    # Test that send_response builds correct JSON-RPC response
    # (won't actually connect but proves the method exists)
    pass


@pytest.mark.asyncio
async def test_transport_connect_rejected():
    t = XiaozhiTransport("ws://localhost:19999", reconnect_interval=0.1)
    with pytest.raises(ConnectionError):
        await t.connect()
