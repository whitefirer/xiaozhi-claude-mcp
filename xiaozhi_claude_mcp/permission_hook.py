#!/usr/bin/env python3
"""
PreToolUse hook for Claude Code.

Reads tool call info from stdin (Claude Code passes JSON with tool_name, tool_input).
Writes permission request file for MCP Server.
Polls for result file. Exits 0 (allow) or 2 (block).
"""
import json
import os
import sys
import time
import logging

logging.basicConfig(
    level=logging.DEBUG,
    filename="/tmp/claude-xiaozhi-perms/hook.log",
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger("permission_hook")

PERM_DIR = "/tmp/claude-xiaozhi-perms"
POLL_MS = 200
TIMEOUT_S = 86400


def main():
    os.makedirs(PERM_DIR, exist_ok=True)

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

    result_path = os.path.join(PERM_DIR, f"{permission_id}.result.json")
    deadline = time.time() + TIMEOUT_S

    while time.time() < deadline:
        if os.path.exists(result_path):
            try:
                with open(result_path) as f:
                    result = json.loads(f.read())
                decision = result.get("decision", False)
                logger.info("Decision: %s = %s", permission_id, decision)
                for p in (req_path, result_path):
                    if os.path.exists(p):
                        os.unlink(p)
                if decision:
                    sys.exit(0)
                else:
                    sys.exit(2)
            except (json.JSONDecodeError, KeyError):
                pass
        time.sleep(POLL_MS / 1000.0)

    logger.warning("Timeout for %s", permission_id)
    for p in (req_path, result_path):
        if os.path.exists(p):
            os.unlink(p)
    sys.exit(2)


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
