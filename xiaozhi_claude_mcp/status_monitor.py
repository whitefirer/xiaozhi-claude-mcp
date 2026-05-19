from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")


@dataclass
class Heartbeat:
    total: int = 0
    running: int = 0
    waiting: int = 0
    msg: str = ""
    entries: list[str] = field(default_factory=list)
    tokens: int = 0
    tokens_today: int = 0
    prompt: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "total": self.total,
            "running": self.running,
            "waiting": self.waiting,
            "msg": self.msg,
            "entries": self.entries,
            "tokens": self.tokens,
            "tokens_today": self.tokens_today,
        }
        if self.prompt:
            d["prompt"] = self.prompt
        return d


class StatusMonitor:
    def __init__(self, claude_binary: str = "claude"):
        self.claude_binary = claude_binary
        self._last_heartbeat = Heartbeat()

    def get_heartbeat(self, pending_permissions: list | None = None) -> Heartbeat:
        hb = self._detect_sessions()
        if pending_permissions:
            hb.waiting = len(pending_permissions)
            if pending_permissions:
                first = pending_permissions[0]
                hb.prompt = {
                    "id": first.permission_id,
                    "tool": first.tool,
                    "hint": first.hint,
                }
        self._last_heartbeat = hb
        return hb

    def _detect_sessions(self) -> Heartbeat:
        hb = Heartbeat()
        if not os.path.isdir(SESSIONS_DIR):
            return hb

        active = []
        try:
            for name in os.listdir(SESSIONS_DIR):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(SESSIONS_DIR, name)
                try:
                    with open(path) as f:
                        data = json.load(f)
                    pid = data.get("pid")
                    if pid and self._pid_alive(pid):
                        active.append(data)
                    else:
                        logger.debug("Session %s pid=%s not alive", name, pid)
                except (json.JSONDecodeError, OSError):
                    pass
        except OSError:
            return hb

        hb.total = len(active)
        hb.running = len(active)

        if active:
            latest = max(active, key=lambda d: d.get("startedAt", 0))
            hb.msg = f"{hb.total} session(s)"
            entries = []
            for s in active:
                cwd = s.get("cwd", "")
                kind = s.get("kind", "")
                entries.append(f"{kind} @ {cwd}")
            hb.entries = entries[-8:]

        return hb

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _build_heartbeat(self, raw_output: str) -> Heartbeat:
        try:
            data = json.loads(raw_output)
            result_text = data.get("result", "")
            usage = data.get("usage", {})
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            return Heartbeat(
                running=0,
                msg=result_text[:200] if result_text else "",
                tokens=tokens,
            )
        except (json.JSONDecodeError, KeyError):
            return Heartbeat()
