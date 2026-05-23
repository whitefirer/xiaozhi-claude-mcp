"""
Terminal authentication — password, xiaozhi voice, and xiaozhi display codes.

Self-contained module. No dependency on PTY, transport, or other MCP internals.
Usable standalone for future xiaozhi verification projects.

Session token is a simple random hex string, checked against in-memory dict.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time

logger = logging.getLogger(__name__)

SESSION_TTL = 3600  # 1 hour
VOICE_CODE_TTL = 60  # 1 minute to speak
DISPLAY_CODE_TTL = 300  # 5 minutes to type in
VOICE_CODE_LENGTH = 6
DISPLAY_CODE_LENGTH = 4

# Avoid confusable chars for voice codes (read aloud)
VOICE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1


def _now() -> float:
    return time.time()


# ── session store ──────────────────────────────────────────


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, float] = {}  # token → expiry

    def create(self) -> str:
        token = secrets.token_hex(32)
        self._sessions[token] = _now() + SESSION_TTL
        return token

    def validate(self, token: str) -> bool:
        exp = self._sessions.get(token)
        if exp is None:
            return False
        if _now() > exp:
            del self._sessions[token]
            return False
        return True

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def gc(self) -> int:
        """Remove expired sessions, return count remaining."""
        now = _now()
        expired = [t for t, e in self._sessions.items() if now > e]
        for t in expired:
            del self._sessions[t]
        return len(self._sessions)


# ── challenge stores ───────────────────────────────────────


class VoiceChallengeStore:
    """Web shows code → user speaks to xiaozhi → xiaozhi approves."""

    def __init__(self):
        self._pending: dict[str, dict] = {}  # code → {challenge_id, display, expiry, approved}
        self._by_challenge: dict[str, dict] = {}  # challenge_id → same entry

    def create(self) -> dict:
        """Returns {challenge_id, display, expires_in}."""
        code = "".join(secrets.choice(VOICE_ALPHABET) for _ in range(VOICE_CODE_LENGTH))
        display = code  # no dash: easier to speak "X S K 7 F 3"
        challenge_id = secrets.token_hex(16)
        entry = {
            "challenge_id": challenge_id,
            "display": display,
            "code": code,
            "expiry": _now() + VOICE_CODE_TTL,
            "approved": False,
        }
        self._pending[code] = entry
        self._by_challenge[challenge_id] = entry
        logger.info("Voice challenge created: %s (id=%s)", display, challenge_id[:8])
        return {"challenge_id": challenge_id, "display": display, "expires_in": VOICE_CODE_TTL}

    def check(self, challenge_id: str) -> str | None:
        """Poll: has this challenge been approved? Returns session_token or None."""
        self.gc()
        entry = self._by_challenge.get(challenge_id)
        if entry is None:
            return None
        if entry.get("approved"):
            token = entry.get("session_token")
            self._cleanup(entry)
            return token
        return None

    def approve(self, user_input: str) -> tuple[str | None, str | None]:
        """Called by xiaozhi MCP tool.
        Returns (challenge_id, None) on success, (None, error_type) on failure.
        error_type is 'invalid' or 'expired'.
        """
        self.gc()
        cleaned = user_input.strip().upper().replace("-", "").replace(" ", "")
        entry = self._pending.get(cleaned)
        if entry is None:
            return None, "invalid"
        if _now() > entry["expiry"]:
            self._cleanup(entry)
            return None, "expired"
        entry["approved"] = True
        logger.info("Voice challenge approved: %s (id=%s)", entry["display"], entry["challenge_id"][:8])
        return entry["challenge_id"], None

    def set_session_token(self, challenge_id: str, token: str) -> None:
        """Store session token for the polling frontend to pick up."""
        entry = self._by_challenge.get(challenge_id)
        if entry:
            entry["session_token"] = token

    def _cleanup(self, entry: dict) -> None:
        self._pending.pop(entry.get("code"), None)
        self._by_challenge.pop(entry.get("challenge_id"), None)

    def gc(self) -> None:
        now = _now()
        for code in list(self._pending):
            if self._pending[code]["expiry"] < now:
                self._cleanup(self._pending[code])


class DisplayChallengeStore:
    """Xiaozhi shows code → user types into web."""

    def __init__(self):
        self._pending: dict[str, dict] = {}  # code → {expiry}

    def create(self) -> str:
        code = "".join(secrets.choice("0123456789") for _ in range(DISPLAY_CODE_LENGTH))
        self._pending[code] = {"expiry": _now() + DISPLAY_CODE_TTL}
        logger.info("Display challenge created: %s", code)
        return code

    def verify(self, user_input: str) -> bool:
        cleaned = user_input.strip()
        entry = self._pending.pop(cleaned, None)
        if entry is None:
            return False
        if _now() > entry["expiry"]:
            return False
        return True

    def gc(self) -> None:
        now = _now()
        for code in list(self._pending):
            if self._pending[code]["expiry"] < now:
                del self._pending[code]


# ── password ───────────────────────────────────────────────

_nonces: dict[str, float] = {}  # nonce → expiry
NONCE_TTL = 60


def _make_nonce() -> str:
    nonce = secrets.token_hex(16)
    _nonces[nonce] = _now() + NONCE_TTL
    # Garbage collect expired
    for n in list(_nonces):
        if _nonces[n] < _now():
            del _nonces[n]
    return nonce


def check_password_hash(config_password: str, nonce: str, hash_value: str) -> bool:
    """Constant-time: sha256(nonce + password) == hash_value."""
    if not config_password or nonce not in _nonces:
        return False
    del _nonces[nonce]  # one-time use
    expected = hashlib.sha256(f"{nonce}{config_password}".encode()).hexdigest()
    return secrets.compare_digest(expected, hash_value)


# ── unified auth manager ───────────────────────────────────


class TerminalAuth:
    """Manages all terminal auth methods.

    Usage:
        auth = TerminalAuth(password="secret123")
        hook_server.register_auth(auth)
    """

    def __init__(self, password: str = "", enable_voice: bool = False,
                 enable_display: bool = False):
        self._password = password
        self._enable_voice = enable_voice
        self._enable_display = enable_display
        self.sessions = SessionStore()
        self.voice = VoiceChallengeStore() if enable_voice else None
        self.display = DisplayChallengeStore() if enable_display else None

    @property
    def has_password(self) -> bool:
        return bool(self._password)

    @property
    def has_voice(self) -> bool:
        return self._enable_voice and self.voice is not None

    @property
    def has_display(self) -> bool:
        return self._enable_display and self.display is not None

    # ── password ────────────────────────────────────────

    def create_nonce(self) -> str:
        return _make_nonce()

    def login_password(self, nonce: str, hash_value: str) -> str | None:
        """Verify sha256(nonce + password) and return session token."""
        if check_password_hash(self._password, nonce, hash_value):
            return self.sessions.create()
        return None

    # ── voice (web → speak → xiaozhi) ────────────────────

    def create_voice_challenge(self) -> dict | None:
        """Returns {challenge_id, display, expires_in} or None if disabled."""
        if not self.voice:
            return None
        self.voice.gc()
        return self.voice.create()

    def check_voice_challenge(self, challenge_id: str) -> str | None:
        if not self.voice:
            return None
        return self.voice.check(challenge_id)

    def approve_voice(self, spoken_code: str) -> tuple[str | None, str | None]:
        """Returns (challenge_id, None) on success, (None, error_type) on failure."""
        if not self.voice:
            return None, "disabled"
        challenge_id, error = self.voice.approve(spoken_code)
        if challenge_id:
            token = self.sessions.create()
            self.voice.set_session_token(challenge_id, token)
            return challenge_id, None
        return None, error

    # ── display (xiaozhi → screen → web) ─────────────────

    def request_display_challenge(self) -> dict | None:
        if not self.display:
            return None
        self.display.gc()
        code = self.display.create()
        return {
            "ok": True,
            "code": code,
            "display": f"🔇 {code}（{DISPLAY_CODE_TTL//60}分钟有效）",
            "spoken": "验证码已显示在屏幕上，请查看后输入网页",
            "expires_in": DISPLAY_CODE_TTL,
        }

    def verify_display_code(self, code: str) -> str | None:
        if not self.display:
            return None
        if self.display.verify(code):
            return self.sessions.create()
        return None

    # ── session ──────────────────────────────────────────

    def check_session(self, token: str) -> bool:
        return self.sessions.validate(token)

    def gc(self) -> None:
        self.voice.gc()
        self.display.gc()
        self.sessions.gc()
