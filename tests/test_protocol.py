import json
from xiaozhi_claude_mcp.protocol import (
    encode_envelope,
    decode_envelope,
    encode_jsonrpc_request,
    encode_jsonrpc_response,
    encode_jsonrpc_error,
    encode_jsonrpc_notification,
    parse_jsonrpc,
    JsonRpcRequest,
    JsonRpcResponse,
    MCPEnvelope,
)


def test_encode_decode_envelope():
    payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {}}
    env = encode_envelope("sess_001", payload)
    text = json.dumps(env)
    parsed = json.loads(text)

    decoded = decode_envelope(parsed)
    assert decoded.session_id == "sess_001"
    assert decoded.type == "mcp"
    assert decoded.payload == payload


def test_encode_jsonrpc_request():
    req = encode_jsonrpc_request("tools/list", {"cursor": ""}, id=2)
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "tools/list"
    assert req["params"] == {"cursor": ""}
    assert req["id"] == 2


def test_encode_jsonrpc_response():
    resp = encode_jsonrpc_response(id=2, result={"tools": []})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    assert resp["result"] == {"tools": []}
    assert "error" not in resp


def test_encode_jsonrpc_error():
    resp = encode_jsonrpc_error(id=2, code=-32600, message="Invalid Request")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    assert resp["error"]["code"] == -32600
    assert resp["error"]["message"] == "Invalid Request"


def test_encode_jsonrpc_notification():
    notif = encode_jsonrpc_notification(
        "notifications/claude_turn",
        {"content": "hello"}
    )
    assert notif["jsonrpc"] == "2.0"
    assert notif["method"] == "notifications/claude_turn"
    assert "id" not in notif


def test_parse_jsonrpc_request():
    raw = {"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {"name": "claude.status"}}
    parsed = parse_jsonrpc(raw)
    assert isinstance(parsed, JsonRpcRequest)
    assert parsed.method == "tools/call"
    assert parsed.id == 1


def test_parse_jsonrpc_response():
    raw = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}}
    parsed = parse_jsonrpc(raw)
    assert isinstance(parsed, JsonRpcResponse)
    assert parsed.id == 1
    assert parsed.result is not None
    assert parsed.error is None
