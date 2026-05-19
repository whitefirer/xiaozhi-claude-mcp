# 小智 Claude Code MCP Server — Design Spec

## Overview

MCP server that connects to xiaozhi.me's MCP access point, exposing Claude Code management tools. 小智 ESP32 device acts as a physical companion for Claude Code — displaying session state, handling permission approvals, and enabling voice-driven conversation.

Modeled after claude-desktop-buddy, but uses MCP over WebSocket instead of BLE.

## Architecture

```
Claude Code CLI ←→ MCP Server (PC) ←─WSS─→ xiaozhi.me MCP access point ←→ 小智 backend ←→ 小智 ESP32
```

- MCP Server connects OUT to xiaozhi.me's WebSocket endpoint (obtained from xiaozhi.me console)
- MCP Server manages Claude Code via subprocess (`claude` CLI)
- 小智 backend discovers and calls tools through the MCP access point
- 小智 ESP32 displays state and captures user input (voice, buttons)

## Transport

xiaozhi MCP envelope format over WebSocket:

```json
{
  "session_id": "...",
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": { "name": "claude.status", "arguments": {} },
    "id": 1
  }
}
```

Connection is initiated by MCP Server → xiaozhi.me access point. Reconnect on disconnect with configurable interval.

## Tools

| Tool | Direction | Purpose |
|------|-----------|---------|
| `claude.status` | backend→server | Session counts, token stats, recent output, pending permissions |
| `claude.send_message` | backend→server | Send prompt to Claude Code, get response |
| `claude.approve` | backend→server | Approve a pending permission request |
| `claude.deny` | backend→server | Deny a pending permission request |
| `claude.notify_permission` | server→backend (notification) | Push permission request to 小智 |
| `claude.notify_turn` | server→backend (notification) | Push completed Claude turn to 小智 |

### Tool Schemas

**claude.status** — returns heartbeat snapshot (modeled after buddy's heartbeat):

```json
{
  "total": 3,
  "running": 1,
  "waiting": 1,
  "msg": "Analyzing code...",
  "entries": ["recent output line 1", "recent output line 2"],
  "tokens": 125000,
  "tokens_today": 5000,
  "prompt": { "id": "req_abc", "tool": "bash", "hint": "rm -rf /tmp/*" }
}
```

**claude.send_message** — send prompt and get response:

```json
// Input
{ "prompt": "What does this code do?", "session_id": "optional-session-id" }

// Output
{ "content": "This code handles...", "session_id": "sess_001", "tokens": 1500 }
```

**claude.approve / claude.deny** — permission decisions:

```json
// Input
{ "permission_id": "req_abc" }

// Output
{ "ok": true }
```

**claude.notify_permission** — push notification:

```json
{
  "permission_id": "req_abc",
  "tool": "bash",
  "hint": "rm -rf /tmp/*"
}
```

**claude.notify_turn** — push notification:

```json
{
  "role": "assistant",
  "content": "I'll analyze this code...",
  "session_id": "sess_001",
  "tokens": 1500
}
```

## Permission Approval Flow

Uses file semaphore pattern because Claude Code PreToolUse hooks are synchronous with a configurable timeout (set to 86400s).

```
1. Claude Code wants to run Bash("rm -rf /tmp/*")
2. PreToolUse hook triggered → writes /tmp/claude-xiaozhi-perms/{session_id}.json
3. MCP Server detects new file → sends claude.notify_permission to 小智
4. 小智 displays "Allow rm -rf /tmp/*?" on screen
5. User presses button A (approve) or B (deny) on 小智
6. 小智 backend calls claude.approve or claude.deny
7. MCP Server writes /tmp/claude-xiaozhi-perms/{session_id}.result.json
8. Hook polls for result → exit 0 (allow) or exit 2 (deny)
```

Hook timeout: 86400s (24h). Hook polls result file every 200ms. On timeout: exit 2 (deny).

File naming uses session_id prefix to avoid cross-session race conditions.

## Claude Code Integration

### send_message

```bash
claude -p "<prompt>"                    # one-shot
claude --resume <session_id> -p "<...>" # continue session
```

stdout captured and returned as response content.

### status monitoring

Poll `~/.claude/sessions/` directory for active session metadata. Parse output of `claude status` if available.

### permission hook (PreToolUse)

Configured in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": "python /path/to/xiaozhi-claude-mcp/permission_hook.py",
          "timeout": 86400
        }]
      }
    ]
  }
}
```

## Configuration

```yaml
# config.yaml
server:
  xiaozhi_endpoint: "wss://xiaozhi.me/mcp/agent/xxx"
  reconnect_interval: 5

claude:
  binary: "claude"
  permission_hook_timeout: 86400
  perm_dir: "/tmp/claude-xiaozhi-perms"

status:
  poll_interval_sec: 5
```

## Project Structure

```
xiaozhi-claude-mcp/
├── server.py              # WebSocket + MCP main process
├── claude_driver.py       # subprocess wrapper for claude CLI
├── permission_broker.py   # file semaphore read/write
├── status_monitor.py      # polls ~/.claude/sessions/
├── permission_hook.py     # PreToolUse hook script
├── mcp_tools.py           # tool registration + handlers
├── config.yaml            # configuration
└── requirements.txt       # Python dependencies
```

## Dependencies

- Python 3.10+
- `websockets` — WebSocket client for xiaozhi.me connection
- `pyyaml` — config parsing

## Open Questions

- How exactly does `claude status` expose session metadata? May need to fall back to filesystem polling.
- Does xiaozhi.me require authentication on the MCP access point (token, API key)?
- Claude Code `--resume` session ID format — need to verify.

## Future

- Replace file semaphore with direct MCP-based permission interception (approach B from brainstorming)
- Stream Claude Code output tokens in real-time via notifications
- Support multiple concurrent Claude Code sessions
