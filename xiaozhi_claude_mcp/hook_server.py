"""
HTTP server that receives Claude Code hook callbacks + serves web terminal.

Runs on port 9999.
  POST /hooks/claude-stop       — Claude completed a response
  POST /hooks/claude-stop-failure — Claude errored
  GET  /                        — xterm.js web terminal
  WS   /ws                      — WebSocket mirroring PTY output
"""
from __future__ import annotations

import asyncio
import logging
import os
import json
from aiohttp import web

logger = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")


class HookServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._pty_session = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_pty_session(self, session) -> None:
        self._pty_session = session
        # Stream PTY output to all web terminal clients
        session.set_output_callback(self._on_pty_output)

    def _on_pty_output(self, data: bytes) -> None:
        """Called from PTY reader thread on every output chunk."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda d=data: asyncio.ensure_future(self._broadcast_raw(d))
        )

    async def _broadcast_raw(self, data: bytes) -> None:
        """Send raw bytes to xterm.js — no decode, no replacement characters."""
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._app.router.add_post("/hooks/claude-stop", self._handle_stop)
        self._app.router.add_post("/hooks/claude-stop-failure", self._handle_stop_failure)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/ws", self._handle_ws)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Hook server + terminal: http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ── hook callbacks ─────────────────────────────────────

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if self._pty_session is None:
            return web.json_response({"status": "no session"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        content = body.get("content", "")
        sid = body.get("session_id", "")
        logger.info("Stop hook fired, content=%d chars session=%s",
                    len(content), sid[:8] if sid else "?")

        pty_sid = self._pty_session.session_id
        if pty_sid and sid and sid != pty_sid:
            logger.warning(
                "Ignoring Stop hook from foreign session=%s (ours=%s)",
                sid[:8], pty_sid[:8],
            )
            return web.json_response({"status": "wrong session"}, status=409)

        if sid and not pty_sid:
            # Only bind to first hook during an active turn (PTY BUSY)
            if self._pty_session.state.name != "BUSY":
                logger.info("Ignoring hook during idle (session=%s), waiting for turn",
                           sid[:8])
                return web.json_response({"status": "not waiting"}, status=409)
            self._pty_session.session_id = sid
            logger.info("Bound PTY to session_id: %s", sid[:8])

        self._pty_session.notify_turn_complete(output_hint=content)
        return web.json_response({"status": "ok"})

    async def _handle_stop_failure(self, request: web.Request) -> web.Response:
        if self._pty_session is None:
            return web.json_response({"status": "no session"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = body.get("session_id", "")
        logger.warning("StopFailure hook fired session=%s", sid[:8] if sid else "?")

        pty_sid = self._pty_session.session_id
        if pty_sid and sid and sid != pty_sid:
            logger.warning("Ignoring StopFailure from foreign session=%s", sid[:8])
            return web.json_response({"status": "wrong session"}, status=409)

        self._pty_session.notify_turn_complete(output_hint="[Claude error]")
        return web.json_response({"status": "ok"})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    # ── web terminal ───────────────────────────────────────

    async def _handle_index(self, request: web.Request) -> web.Response:
        path = os.path.join(WEB_DIR, "index.html")
        try:
            with open(path) as f:
                html = f.read()
        except OSError:
            html = "<h1>index.html not found</h1>"
        return web.Response(text=html, content_type="text/html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("Web terminal client connected (%d total)", len(self._ws_clients))

        # Send current PTY buffer (raw bytes for xterm.js)
        if self._pty_session:
            raw = self._pty_session.get_recent_output_bytes()
            if raw:
                await ws.send_bytes(raw)

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT and self._pty_session:
                    # Forward keystrokes to PTY
                    self._pty_session.write_raw(msg.data.encode())
                elif msg.type == web.WSMsgType.BINARY and self._pty_session:
                    self._pty_session.write_raw(msg.data)
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def broadcast_pty(self, text: str) -> None:
        """Push PTY output to all web terminal clients."""
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead
