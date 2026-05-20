from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CONVERSATION_TAIL_LINES = 20


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
    def __init__(self, claude_binary: str = "claude",
                 exclude_paths: list[str] | None = None,
                 exclude_kinds: list[str] | None = None):
        self.claude_binary = claude_binary
        self.exclude_paths = exclude_paths or []
        self.exclude_kinds = exclude_kinds or []
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
                    if not pid or not self._pid_alive(pid):
                        logger.debug("Session %s pid=%s not alive", name, pid)
                        continue

                    cwd = data.get("cwd", "")
                    kind = data.get("kind", "")

                    if any(p in cwd for p in self.exclude_paths):
                        continue
                    if kind in self.exclude_kinds:
                        continue

                    active.append(data)
                except (json.JSONDecodeError, OSError):
                    pass
        except OSError:
            return hb

        hb.total = len(active)
        hb.running = len(active)

        if active:
            latest = max(active, key=lambda d: d.get("startedAt", 0))
            session_id = latest.get("sessionId", "")
            cwd = latest.get("cwd", "")

            hb.msg = f"{hb.total} session(s)"
            if cwd:
                hb.msg += f", latest: {os.path.basename(cwd) if cwd != '/' else '/'}"
            entries = []

            # Collect cwd entries
            for s in active:
                entries.append(s.get("cwd", ""))
            hb.entries = entries[-8:]

            # Read recent conversation for the latest session
            conv_texts = self._read_recent_output(session_id, cwd)
            if conv_texts:
                # Prepend last few assistant outputs to entries
                hb.entries = conv_texts[-5:] + hb.entries

        return hb

    def _read_recent_output(self, session_id: str, cwd: str) -> list[str]:
        """Read last few assistant messages from the session conversation file."""
        if not session_id or not cwd:
            return []
        project_name = _project_hash(cwd)
        conv_path = os.path.join(PROJECTS_DIR, project_name, f"{session_id}.jsonl")
        if not os.path.exists(conv_path):
            return []

        texts = []
        try:
            # Read last N lines for efficiency
            with open(conv_path) as f:
                lines = f.readlines()
            for line in lines[-CONVERSATION_TAIL_LINES:]:
                try:
                    msg = json.loads(line.strip())
                    if msg.get("type") != "assistant":
                        continue
                    content = msg.get("message", {}).get("content", [])
                    text = _extract_text(content)
                    if text:
                        texts.append(text)
                except (json.JSONDecodeError, KeyError):
                    pass
        except OSError:
            pass
        return texts

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


    def _build_heartbeat(self, raw_output: str) -> Heartbeat:
        """Parse claude -p --output-format json output (kept for tests)."""
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


def _project_hash(cwd: str) -> str:
    return cwd.replace("/", "-")


def _extract_text(content) -> str:
    """Extract human-readable text from Claude message content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type", "")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    hint = str(inp.get("command", inp.get("file_path", str(inp))))[:80]
                else:
                    hint = str(inp)[:80]
                parts.append(f"[{name}] {hint}")
            elif t == "thinking":
                th = block.get("thinking", "")
                parts.append(th)
        return " ".join(parts)
    return ""
