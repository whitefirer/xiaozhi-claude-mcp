from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum, auto

import websockets
from websockets.asyncio.client import ClientConnection

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
        self._recv_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        self.state = TransportState.DISCONNECTED

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

    async def send(self, msg: dict) -> None:
        if not self._ws or self.state != TransportState.CONNECTED:
            raise RuntimeError("Not connected")
        text = json.dumps(msg)
        await self._ws.send(text)

    async def send_response(self, req_id, result: dict) -> None:
        await self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def send_error(self, req_id, code: int, message: str) -> None:
        await self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    async def send_notification(self, method: str, params: dict) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def recv(self) -> dict:
        return await self._recv_queue.get()

    async def _recv_loop(self) -> None:
        while self._ws and self.state == TransportState.CONNECTED:
            try:
                msg = await self._ws.recv()
                raw = json.loads(msg)
                await self._recv_queue.put(raw)
            except websockets.ConnectionClosed:
                logger.warning("Connection closed")
                self.state = TransportState.DISCONNECTED
                break
            except Exception as e:
                logger.error("Recv error: %s", e)
