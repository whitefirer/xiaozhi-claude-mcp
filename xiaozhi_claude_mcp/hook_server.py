"""
HTTP server that receives Claude Code hook callbacks + serves web terminal.

  POST /hooks/claude-stop         — Claude completed a response
  POST /hooks/claude-stop-failure — Claude errored
  GET  /health                    — liveness check

Terminal auth endpoints (when show_terminal=true):
  GET  /login                     — login page (password / xiaozhi voice / xiaozhi display)
  POST /api/login/password        — password login → session cookie
  POST /api/login/voice/create    — create voice challenge (web→speak→xiaozhi)
  GET  /api/login/voice/check     — poll: did xiaozhi approve?
  GET  /api/login/display/status  — poll: did xiaozhi create a display code?
  POST /api/login/display/verify  — verify display code from xiaozhi
  GET  /api/login/session         — check if session is valid
  GET  /                          — xterm.js web terminal (requires session or token)
  WS   /ws                        — WebSocket mirroring PTY output (requires session or token)
"""
from __future__ import annotations

import asyncio
import logging
import os
import json
from aiohttp import web

logger = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

COOKIE_NAME = "xz_term_sess"
_LOCALHOST = {"127.0.0.1", "::1"}


def _is_local(request: web.Request) -> bool:
    peername = request.transport.get_extra_info("peername")
    if peername is None:
        return False
    return peername[0] in _LOCALHOST


def _read_cookie(request: web.Request) -> str:
    return request.cookies.get(COOKIE_NAME, "")


def _set_cookie(response: web.Response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Strict",
                        max_age=3600)


# ── HookServer ────────────────────────────────────────────────


class HookServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999,
                 show_terminal: bool = True, allow_terminal_input: bool = True,
                 terminal_token: str = "", allow_token: bool = True,
                 terminal_auth=None, auth_redirect_url: str = ""):
        self.host = host
        self.port = port
        self.show_terminal = show_terminal
        self.allow_terminal_input = allow_terminal_input
        self.terminal_token = terminal_token
        self._allow_token = allow_token
        self._auth = terminal_auth
        self._auth_redirect_url = auth_redirect_url
        self._redirect_sessions: set[str] = set()  # auth server tokens in redirect mode
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._pty_session = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_pty_session(self, session) -> None:
        self._pty_session = session
        if self.show_terminal:
            session.set_output_callback(self._on_pty_output)

    def _on_pty_output(self, data: bytes) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda d=data: asyncio.ensure_future(self._broadcast_raw(d))
        )

    async def _broadcast_raw(self, data: bytes) -> None:
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._app.router.add_post("/hooks/claude-stop", self._handle_stop)
        self._app.router.add_post("/hooks/claude-stop-failure", self._handle_stop_failure)
        self._app.router.add_get("/health", self._handle_health)
        if self.show_terminal:
            self._register_terminal_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        has_auth = self._auth is not None
        logger.info("Hook server: http://%s:%d terminal=%s input=%s auth=%s",
                    self.host, self.port, self.show_terminal,
                    self.allow_terminal_input, has_auth)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def _register_terminal_routes(self) -> None:
        r = self._app.router
        r.add_get("/login", self._handle_login_page)
        r.add_get("/api/login/challenge", self._handle_login_challenge)
        r.add_post("/api/login/password", self._handle_login_password)
        r.add_post("/api/login/voice/create", self._handle_voice_create)
        r.add_get("/api/login/voice/check", self._handle_voice_check)
        r.add_get("/api/login/display/status", self._handle_display_status)
        r.add_post("/api/login/display/verify", self._handle_display_verify)
        r.add_post("/api/login/logout", self._handle_logout)
        r.add_get("/api/login/session", self._handle_session_check)
        r.add_get("/", self._handle_index)
        r.add_get("/ws", self._handle_ws)

    # ── auth helpers ───────────────────────────────────────

    def _has_terminal_access(self, request: web.Request) -> bool:
        """Check if request has terminal access."""
        if self._auth_redirect_url:
            return _read_cookie(request) in self._redirect_sessions
        if self._auth and self._auth.check_session(_read_cookie(request)):
            return True
        if self._allow_token and self._check_terminal_token(request):
            return True
        if self._auth is None and not self.terminal_token:
            return True
        return False

    def _check_terminal_token(self, request: web.Request) -> bool:
        if not self.terminal_token:
            return False
        return request.query.get("token", "") == self.terminal_token

    # ── hook callbacks ─────────────────────────────────────

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if not _is_local(request):
            return web.json_response({"status": "forbidden"}, status=403)
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
            logger.warning("Ignoring Stop hook from foreign session=%s (ours=%s)",
                           sid[:8], pty_sid[:8])
            return web.json_response({"status": "wrong session"}, status=409)

        if sid and not pty_sid:
            if self._pty_session.state.name != "BUSY":
                logger.info("Ignoring hook during idle (session=%s), waiting for turn",
                           sid[:8])
                return web.json_response({"status": "not waiting"}, status=409)
            self._pty_session.session_id = sid
            logger.info("Bound PTY to session_id: %s", sid[:8])

        self._pty_session.notify_turn_complete(output_hint=content)
        return web.json_response({"status": "ok"})

    async def _handle_stop_failure(self, request: web.Request) -> web.Response:
        if not _is_local(request):
            return web.json_response({"status": "forbidden"}, status=403)
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

    # ── login page ─────────────────────────────────────────

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        if self._auth_redirect_url:
            back = f"http://{request.host}/"
            raise web.HTTPFound(
                f"{self._auth_redirect_url}/login?redirect_uri={back}"
                f"&title=Claude Code 终端")
        path = os.path.join(WEB_DIR, "login.html")
        try:
            with open(path) as f:
                html = f.read()
        except OSError:
            html = "<h1>login.html not found</h1>"
        # Inject auth config
        cfg = {
            "hasPassword": bool(self._auth and self._auth.has_password),
            "hasVoice": bool(self._auth),
            "hasDisplay": bool(self._auth),
        }
        html = html.replace("<!-- AUTH_CONFIG -->",
                            f"<script>window.__AUTH__ = {json.dumps(cfg)};</script>")
        return web.Response(text=html, content_type="text/html")

    # ── password login ─────────────────────────────────────

    async def _handle_login_challenge(self, request: web.Request) -> web.Response:
        if not self._auth or not self._auth.has_password:
            return web.json_response({"ok": False, "error": "密码登录未启用"}, status=400)
        nonce = self._auth.create_nonce()
        return web.json_response({"ok": True, "nonce": nonce})

    async def _handle_login_password(self, request: web.Request) -> web.Response:
        if not self._auth or not self._auth.has_password:
            return web.json_response({"ok": False, "error": "密码登录未启用"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        token = self._auth.login_password(body.get("nonce", ""), body.get("hash", ""))
        if token:
            resp = web.json_response({"ok": True})
            _set_cookie(resp, token)
            return resp
        return web.json_response({"ok": False, "error": "密码错误"}, status=401)

    # ── voice challenge (web → speak → xiaozhi) ────────────

    async def _handle_voice_create(self, request: web.Request) -> web.Response:
        if not self._auth:
            return web.json_response({"ok": False, "error": "小智验证未启用"}, status=400)
        challenge = self._auth.create_voice_challenge()
        return web.json_response({"ok": True, **challenge})

    async def _handle_voice_check(self, request: web.Request) -> web.Response:
        if not self._auth:
            return web.json_response({"ok": False}, status=400)
        challenge_id = request.query.get("challenge_id", "")
        if not challenge_id:
            return web.json_response({"approved": False})
        token = self._auth.check_voice_challenge(challenge_id)
        if token:
            resp = web.json_response({"approved": True})
            _set_cookie(resp, token)
            return resp
        return web.json_response({"approved": False})

    # ── display challenge (xiaozhi → screen → web) ──────────

    async def _handle_display_status(self, request: web.Request) -> web.Response:
        if not self._auth:
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True, "status": "pending"})

    async def _handle_display_verify(self, request: web.Request) -> web.Response:
        if not self._auth:
            return web.json_response({"ok": False, "error": "小智验证未启用"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        token = self._auth.verify_display_code(body.get("code", ""))
        if token:
            resp = web.json_response({"ok": True})
            _set_cookie(resp, token)
            return resp
        return web.json_response({"ok": False, "error": "验证码错误或已过期"}, status=401)

    # ── logout ────────────────────────────────────────────

    async def _handle_logout(self, request: web.Request) -> web.Response:
        if self._auth:
            self._auth.sessions.revoke(_read_cookie(request))
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE_NAME)
        return resp

    # ── session check ──────────────────────────────────────

    async def _handle_session_check(self, request: web.Request) -> web.Response:
        valid = False
        if self._auth:
            valid = self._auth.check_session(_read_cookie(request))
        return web.json_response({"ok": valid})

    # ── web terminal ───────────────────────────────────────

    async def _handle_index(self, request: web.Request) -> web.Response:
        # Handle redirect-mode token callback
        token = request.query.get("token", "")
        if token and self._auth_redirect_url:
            from aiohttp import ClientSession
            try:
                async with ClientSession() as sess:
                    async with sess.get(
                        f"{self._auth_redirect_url}/api/verify?token={token}"
                    ) as r:
                        data = await r.json()
                if data.get("ok"):
                    self._redirect_sessions.add(token)
                    resp = web.HTTPFound("/")
                    resp.set_cookie("xz_term_sess", token, httponly=True,
                                    samesite="Strict", max_age=3600)
                    return resp
            except Exception:
                pass

        if not self._has_terminal_access(request):
            if self._auth_redirect_url:
                back = f"http://{request.host}/"
                raise web.HTTPFound(
                    f"{self._auth_redirect_url}/login?redirect_uri={back}"
                    f"&title=Claude Code 终端")
            raise web.HTTPFound("/login")
        path = os.path.join(WEB_DIR, "index.html")
        try:
            with open(path) as f:
                html = f.read()
        except OSError:
            html = "<h1>index.html not found</h1>"
        return web.Response(text=html, content_type="text/html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._has_terminal_access(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4003, message=b"unauthorized")
            return ws
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("Web terminal client connected (%d total)", len(self._ws_clients))

        if self._pty_session:
            raw = self._pty_session.get_recent_output_bytes()
            if raw:
                await ws.send_bytes(raw)

        try:
            async for msg in ws:
                if not self.allow_terminal_input or not self._pty_session:
                    continue
                if msg.type == web.WSMsgType.TEXT:
                    self._pty_session.write_raw(msg.data.encode())
                elif msg.type == web.WSMsgType.BINARY:
                    self._pty_session.write_raw(msg.data)
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def broadcast_pty(self, text: str) -> None:
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead
