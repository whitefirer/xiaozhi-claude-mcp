# 小智 Claude Code MCP Server — Design Spec

## Overview

MCP server that connects to xiaozhi.me's MCP access point, exposing Claude Code management tools. 小智 ESP32 device acts as a physical companion for Claude Code — displaying session state, handling permission approvals, and enabling voice-driven conversation.

## Architecture

```
Claude Code PTY ←→ MCP Server (PC) ←─WSS─→ xiaozhi.me MCP access point ←→ 小智 backend ←→ 小智 ESP32
                      │
                      ├─ Hook Server (:9999) ← POST ─ Stop hook (captures turn output)
                      │                          └ PermissionRequest hook (records requests)
                      └─ PTY Session (persistent claude process, no cold start)
```

- MCP Server connects OUT to xiaozhi.me's WebSocket endpoint
- MCP Server manages Claude Code via persistent PTY session (forked `claude` process)
- Hook server (aiohttp on :9999) receives Stop and PermissionRequest hook callbacks
- Stop hook captures Claude's turn output via POST to hook server
- PermissionRequest hook writes request files to shared perm_dir
- 小智 backend discovers and calls tools through the MCP access point

## Tools

| Tool | Direction | Purpose |
|------|-----------|---------|
| `claude.status` | backend→server | Session counts, pending tasks, permission requests, PTY state |
| `claude.send_message` | backend→server | Send prompt to PTY Claude, returns task_id immediately (async) |
| `claude.get_result` | backend→server | Poll async task result (status + content + preview) |
| `claude.approve` | backend→server | Approve pending permission, sends "1\r" keystroke to PTY |
| `claude.deny` | backend→server | Deny pending permission, sends "3\r" keystroke to PTY |
| `claude.notify_turn` | server→backend | Push completed Claude turn to 小智 (notification) |

### claude.send_message (async task model)

```
Input:  { "prompt": "...", "session_id": "", "max_turns": 2 }
Output: { "status": "processing", "task_id": "a1b2c3d4" }
```

Prompt is written to PTY. Returns immediately with task_id. Background task waits for Stop hook to capture response. Use `claude.get_result(task_id)` to fetch result.

### claude.get_result

```
Input:  { "task_id": "a1b2c3d4" }
Output pending: { "status": "pending", "preview": "<recent PTY output>" }
Output done:    { "status": "done", "content": "...", "session_id": "...", "task_id": "..." }
Output error:   { "status": "error", "message": "..." }
Output not_found: { "status": "not_found" }
```

Done results are marked consumed and won't appear in subsequent claude.status calls.

### claude.status

```json
{
  "total": 2,
  "running": 2,
  "waiting": 1,
  "pending_tasks": 0,
  "completed_task_ids": ["a1b2c3d4"],
  "permission_requests": [
    {"permission_id": "sid-Bash-123", "tool": "Bash", "hint": "rm -rf /tmp/*"}
  ],
  "pty": {"state": "idle", "pid": 12345, "session_id": "abc123"},
  "entries": ["recent output line 1", "recent output line 2"]
}
```

### claude.approve / claude.deny

```
Input:  { "permission_id": "sid-Bash-123" }
Output: { "ok": true }
```

Writes result file and sends PTY keystroke (1=approve, 3=deny) to answer Claude Code's native permission dialog.

## Permission Approval Flow

Uses PermissionRequest hook (record-only, non-blocking) + PTY keystroke simulation.

```
1. PTY Claude wants to run Bash("rm -rf /tmp/*")
2. Claude Code checks built-in permissions → not in allowlist
3. PermissionRequest hook fires → writes /tmp/claude-xiaozhi-perms/{perm_id}.json
4. Hook exits 0 (no hookSpecificOutput) → Claude Code shows native permission dialog in PTY
5. MCP Server detects new file via claude.status polling → shows "waiting: 1"
6. 小智 displays permission request (tool + hint) on screen
7. User approves (press A) or denies (press B) on 小智
8a. APPROVE: 小智 calls claude.approve(perm_id) → server sends "1\r" to PTY → dialog approved
8b. DENY:    小智 calls claude.deny(perm_id) → server sends "3\r" to PTY → dialog denied
```

### Key design decisions

- **PermissionRequest over PreToolUse**: PreToolUse fires for ALL matching tool calls regardless of whether Claude Code would show a dialog. PermissionRequest only fires when a dialog would actually appear, respecting built-in allowlists.

- **Non-blocking hook**: Hook writes request file and exits 0 immediately. Does NOT poll for result. The PTY keystroke simulation is the single gate.

- **Env var guard**: PTY Claude is launched with `XIAOZHI_PERMISSION_HOOK=1`. The permission hook checks this env var — if absent (interactive Claude), exits 0 immediately without recording anything. Prevents noise from the interactive Claude session.

- **Keystroke simulation**: approve sends `1\r` (approve once), deny sends `3\r` (deny). Using `asyncio.create_task` with 0.5s delay to let Claude Code render the dialog.

### Hook configuration (~/.claude/settings.json)

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "python3 /path/to/permission_hook.py"
        }]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": "python3 /path/to/stop_hook.py"
        }]
      }
    ]
  }
}
```

## Claude Code Integration

### PTY Session

Persistent `claude` process via `os.fork()` + `pty.openpty()`. No cold start per request.

- `os.execvp("claude", ["claude"])` — interactive REPL, no `-p` flag
- Env var `XIAOZHI_PERMISSION_HOOK=1` set before exec for hook session filtering
- Stop hook captures turn output and POSTs to hook server
- Hook server (aiohttp, port 9999) binds PTY to session_id on first Stop hook during BUSY state
- Session_id filtering (not PID) prevents cross-session interference

### send_message via PTY

```
1. Write prompt bytes + "\r" to PTY master fd
2. PTY state → BUSY
3. Wait for Stop hook → hook_server receives content via POST
4. Hook server calls notify_turn_complete() → sets _turn_event
5. Cleaned output returned as response content
```

### status monitoring

Poll `~/.claude/sessions/` directory for active session metadata. Parse project session files for recent output display.

## Configuration

```yaml
server:
  xiaozhi_endpoint: "wss://api.xiaozhi.me/mcp/?token=..."
  reconnect_interval: 5

claude:
  perm_dir: "/tmp/claude-xiaozhi-perms"

status:
  poll_interval_sec: 5
  exclude_paths: []
  exclude_kinds: []
```

## Project Structure

```
xiaozhi-claude-mcp/
├── xiaozhi_claude_mcp/
│   ├── server.py              # MCP server main process + tool handlers
│   ├── pty_session.py         # persistent PTY claude subprocess wrapper
│   ├── hook_server.py         # aiohttp server for Stop hook callbacks + web terminal
│   ├── stop_hook.py           # Stop hook script (POSTs content to hook server)
│   ├── permission_hook.py     # PermissionRequest hook script (record-only)
│   ├── permission_broker.py   # file semaphore read/write for permission requests
│   ├── status_monitor.py      # polls ~/.claude/sessions/ for session metadata
│   ├── mcp_tools.py           # tool registration schemas
│   ├── protocol.py            # JSON-RPC 2.0 encode/decode
│   ├── transport.py           # WebSocket transport layer
│   └── config.py              # dataclass config with YAML loading
├── tests/
│   ├── test_e2e.py            # end-to-end MCP + PTY pipeline tests
│   ├── test_permission_broker.py
│   ├── test_protocol.py
│   ├── test_transport.py
│   ├── test_status_monitor.py
│   ├── test_mcp_tools.py
│   ├── test_config.py
│   └── test_integration.py
└── config.yaml
```

## Dependencies

- Python 3.10+
- `websockets` — WebSocket client for xiaozhi.me connection
- `aiohttp` — hook server HTTP endpoints
- `pyyaml` — config parsing
