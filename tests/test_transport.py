import asyncio
import json
import pytest
import pytest_asyncio
import websockets
from xiaozhi_claude_mcp.transport import (
    XiaozhiTransport,
    TransportState,
)
from xiaozhi_claude_mcp.protocol import (
    encode_envelope,
    encode_jsonrpc_request,
)


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
    from xiaozhi_claude_mcp.protocol import MCPEnvelope

    t = XiaozhiTransport(echo_server)
    await t.connect()
    assert t.state == TransportState.CONNECTED

    payload = encode_jsonrpc_request("tools/list", {}, id=1)
    env = MCPEnvelope(session_id="sess_001", type="mcp", payload=payload)

    await t.send(env)

    received = await asyncio.wait_for(t.recv(), timeout=2)
    assert received.session_id == "sess_001"
    assert received.payload["method"] == "tools/list"

    await t.disconnect()
    assert t.state == TransportState.DISCONNECTED


@pytest.mark.asyncio
async def test_transport_connect_rejected():
    t = XiaozhiTransport("ws://localhost:19999", reconnect_interval=0.1)
    with pytest.raises(ConnectionError):
        await t.connect()
