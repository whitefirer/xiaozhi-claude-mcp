from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum, auto

import websockets
from websockets.asyncio.client import ClientConnection

from xiaozhi_claude_mcp.protocol import encode_envelope, decode_envelope, MCPEnvelope

logger = logging.getLogger(__name__)


class TransportState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()


class XiaozhiTransport:
    def __init__(self, endpoint: str, reconnect_interval: float = 5):
        self.endpoint = endpoint
        self.reconnect_interval = reconnect_interval
        self._ws: ClientConnection | None = None
        self._recv_queue: asyncio.Queue[MCPEnvelope] = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        self.state = TransportState.DISCONNECTED
        self._session_id: str = ""

    async def connect(self) -> None:
        self.state = TransportState.CONNECTING
        logger.info("Connecting to %s", self.endpoint)
        try:
            self._ws = await websockets.connect(
                self.endpoint,
                ping_interval=30,
                ping_timeout=10,
            )
        except Exception as e:
            self.state = TransportState.DISCONNECTED
            raise ConnectionError(f"Failed to connect to {self.endpoint}: {e}") from e

        self.state = TransportState.CONNECTED
        self._session_id = ""
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("Connected")

    async def disconnect(self) -> None:
        self.state = TransportState.DISCONNECTED
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected")

    def set_session_id(self, sid: str) -> None:
        self._session_id = sid

    @property
    def session_id(self) -> str:
        return self._session_id

    async def send(self, envelope: MCPEnvelope) -> None:
        if not self._ws or self.state != TransportState.CONNECTED:
            raise RuntimeError("Not connected")
        text = json.dumps(encode_envelope(envelope.session_id, envelope.payload))
        await self._ws.send(text)

    async def send_notification(self, method: str, params: dict) -> None:
        from xiaozhi_claude_mcp.protocol import encode_jsonrpc_notification

        payload = encode_jsonrpc_notification(method, params)
        env = MCPEnvelope(session_id=self._session_id, type="mcp", payload=payload)
        await self.send(env)

    async def send_response(self, req_id, result: dict) -> None:
        from xiaozhi_claude_mcp.protocol import encode_jsonrpc_response

        payload = encode_jsonrpc_response(req_id, result)
        env = MCPEnvelope(session_id=self._session_id, type="mcp", payload=payload)
        await self.send(env)

    async def send_error(self, req_id, code: int, message: str) -> None:
        from xiaozhi_claude_mcp.protocol import encode_jsonrpc_error

        payload = encode_jsonrpc_error(req_id, code, message)
        env = MCPEnvelope(session_id=self._session_id, type="mcp", payload=payload)
        await self.send(env)

    async def recv(self) -> MCPEnvelope:
        return await self._recv_queue.get()

    async def _recv_loop(self) -> None:
        while self._ws and self.state == TransportState.CONNECTED:
            try:
                msg = await self._ws.recv()
                raw = json.loads(msg)
                if isinstance(raw, str):
                    raw = json.loads(raw)
                env = decode_envelope(raw)
                if env.session_id:
                    self._session_id = env.session_id
                await self._recv_queue.put(env)
            except websockets.ConnectionClosed:
                logger.warning("Connection closed")
                self.state = TransportState.DISCONNECTED
                break
            except Exception as e:
                logger.error("Recv error: %s", e)
