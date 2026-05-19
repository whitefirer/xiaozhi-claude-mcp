from dataclasses import dataclass, field
from typing import Any
import json


@dataclass
class MCPEnvelope:
    session_id: str
    type: str
    payload: dict


@dataclass
class JsonRpcRequest:
    method: str
    params: dict = field(default_factory=dict)
    id: Any = None


@dataclass
class JsonRpcNotification:
    method: str
    params: dict = field(default_factory=dict)


@dataclass
class JsonRpcResponse:
    id: Any
    result: dict | None = None
    error: dict | None = None


def encode_envelope(session_id: str, payload: dict) -> dict:
    return {"session_id": session_id, "type": "mcp", "payload": payload}


def decode_envelope(raw: dict) -> MCPEnvelope:
    return MCPEnvelope(
        session_id=raw["session_id"],
        type=raw.get("type", "mcp"),
        payload=raw["payload"],
    )


def decode_message(text: str) -> MCPEnvelope:
    raw = json.loads(text)
    return decode_envelope(raw)


def encode_message(session_id: str, payload: dict) -> str:
    return json.dumps(encode_envelope(session_id, payload))


def encode_jsonrpc_request(method: str, params: dict | None = None, id: Any = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id is not None:
        msg["id"] = id
    return msg


def encode_jsonrpc_response(id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def encode_jsonrpc_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def encode_jsonrpc_notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def parse_jsonrpc(raw: dict) -> JsonRpcRequest | JsonRpcNotification | JsonRpcResponse:
    if "method" in raw and "result" not in raw and "error" not in raw:
        if "id" in raw:
            return JsonRpcRequest(
                method=raw["method"],
                params=raw.get("params", {}),
                id=raw["id"],
            )
        return JsonRpcNotification(
            method=raw["method"],
            params=raw.get("params", {}),
        )
    return JsonRpcResponse(
        id=raw.get("id"),
        result=raw.get("result"),
        error=raw.get("error"),
    )
