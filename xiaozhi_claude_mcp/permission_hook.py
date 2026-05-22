#!/usr/bin/env python3
"""
PermissionRequest hook for Claude Code — record, then ask.

Reads tool call info from stdin, writes permission request file for MCP Server,
returns {behavior: ask} so Claude Code shows its native permission dialog.
The dialog is answered via PTY keystroke (1=approve, 3=deny).
"""
import json
import os
import sys
import time
import logging

PERM_DIR = "/tmp/claude-xiaozhi-perms"

os.makedirs(PERM_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    filename=os.path.join(PERM_DIR, "hook.log"),
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger("permission_hook")


def main():
    if not os.environ.get("XIAOZHI_PERMISSION_HOOK"):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to read stdin: %s", e)
        sys.exit(0)

    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    hint = _make_hint(tool_name, tool_input)
    permission_id = f"{session_id}-{tool_name}-{int(time.time())}"

    req_path = os.path.join(PERM_DIR, f"{permission_id}.json")
    data = {
        "permission_id": permission_id,
        "tool": tool_name,
        "hint": hint,
        "timestamp": time.time(),
        "session_id": session_id,
    }
    with open(req_path, "w") as f:
        json.dump(data, f)
    logger.info("Request written: %s (%s)", permission_id, hint)

    # Exit 0 without hookSpecificOutput — let Claude Code show its native
    # permission dialog. xiaozhi user approves/denies → claude.approve/claude.deny
    # → PTY write_raw("1\r"/"3\r") answers the dialog.
    sys.exit(0)


def _make_hint(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash" and isinstance(tool_input, dict):
        cmd = tool_input.get("command", "")
        return cmd[:80]
    if tool_name in ("Write", "Edit") and isinstance(tool_input, dict):
        fp = tool_input.get("file_path", "")
        return f"{tool_name}: {fp}"
    return f"{tool_name}: {str(tool_input)[:80]}"


if __name__ == "__main__":
    main()
