from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
        hb = Heartbeat()
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
