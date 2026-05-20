#!/usr/bin/env python3
"""
Stop hook for Claude Code.

Fires when Claude completes a response. Reads the assistant's reply
from the session transcript file and POSTs it to the hook server.
"""
import json
import logging
import os
import sys
import urllib.request

logging.basicConfig(
    level=logging.DEBUG,
    filename="/tmp/xiaozhi-stop-hook.log",
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger("stop_hook")

HOOK_URL = "http://127.0.0.1:9999/hooks/claude-stop"
HOOK_FAIL_URL = "http://127.0.0.1:9999/hooks/claude-stop-failure"

try:
    hook_input = json.loads(sys.stdin.read())
except Exception:
    hook_input = {}

is_error = hook_input.get("is_error", False)
transcript_path = hook_input.get("transcript_path", "")

# Extract last assistant message from transcript
content = ""
if transcript_path and os.path.exists(transcript_path):
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
        # Read last N lines and find assistant messages
        for line in reversed(lines[-50:]):
            try:
                msg = json.loads(line.strip())
                if msg.get("type") == "assistant":
                    msg_content = msg.get("message", {}).get("content", [])
                    if isinstance(msg_content, list):
                        parts = []
                        for block in msg_content:
                            if isinstance(block, dict):
                                t = block.get("type", "")
                                if t == "text":
                                    parts.append(block.get("text", ""))
                                elif t == "tool_use":
                                    name = block.get("name", "?")
                                    inp = block.get("input", {})
                                    hint = str(inp.get("command", inp.get("file_path", str(inp))))[:80]
                                    parts.append(f"[Tool: {name}] {hint}")
                            elif isinstance(block, str):
                                parts.append(block)
                        content = "\n".join(parts)
                    elif isinstance(msg_content, str):
                        content = msg_content
                    break  # Got the last assistant message
            except (json.JSONDecodeError, KeyError):
                pass
    except OSError:
        pass

logger.info("transcript=%s exists=%s content_len=%d",
            transcript_path, os.path.exists(transcript_path), len(content))

payload = {
    "stop_reason": hook_input.get("stop_reason", "end_turn"),
    "is_error": is_error,
    "content": content,
    "session_id": hook_input.get("session_id", ""),
    "claude_pid": os.getppid(),  # PID of the Claude process that invoked this hook
}

url = HOOK_FAIL_URL if is_error else HOOK_URL
try:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)
except Exception:
    pass  # Best-effort
