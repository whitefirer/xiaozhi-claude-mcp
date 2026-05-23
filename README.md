# xiaozhi-claude-mcp

MCP server bridging Claude Code to [小智 (xiaozhi)](https://xiaozhi.me) — the ESP32 physical AI companion. XiaoZhi displays Claude Code session state, handles permission approvals, and enables voice-driven conversation.

## Architecture

```
Claude Code PTY ←→ MCP Server (PC) ←─WSS─→ xiaozhi.me ←→ 小智 backend ←→ 小智 ESP32
                      │
                      ├─ Hook Server (:9999) ← POST ─ Stop hook
                      └─ PTY Session (persistent claude process)
```

The MCP server maintains a persistent PTY Claude session — no cold start per request. Two Claude Code hooks drive the integration:

| Hook | Purpose |
|------|---------|
| **PermissionRequest** | Records tool permission requests for xiaozhi approval |
| **Stop** | Captures Claude's turn output when response completes |

Users approve or deny tool calls on xiaozhi. The server simulates PTY keystrokes (`1\r` = approve, `3\r` = deny) to answer Claude Code's native permission dialogs.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Edit config.yaml with your xiaozhi.me endpoint token
# (obtained from xiaozhi.me console → MCP access point)

# Start
python3 -m xiaozhi_claude_mcp.server config.yaml
```

### Hook Installation

Register two hooks in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "python3 /path/to/xiaozhi-claude-mcp/xiaozhi_claude_mcp/permission_hook.py"
        }]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": "python3 /path/to/xiaozhi-claude-mcp/xiaozhi_claude_mcp/stop_hook.py"
        }]
      }
    ]
  }
}
```

- **PermissionRequest** — fires when Claude Code shows a permission dialog (Bash, Write, Edit). Writes request files to `perm_dir` for xiaozhi to pick up. Exits 0 without blocking.
- **Stop** — fires when Claude finishes a turn. POSTs the response content to the hook server for capture.

The `XIAOZHI_PERMISSION_HOOK=1` env var is set automatically by the PTY session launcher. Only the PTY Claude triggers permission recording — your interactive Claude sessions are unaffected.

Set `XIAOZHI_HOOK_PORT` in `~/.claude/settings.json` env section if you change the default port:

```json
"env": {
  "XIAOZHI_HOOK_PORT": "9999"
}
```

### Web Terminal

The hook server also serves a live terminal view at `http://127.0.0.1:<hook_port>` (default 9999):

- `GET /` — xterm.js web terminal showing real-time PTY output
- `WS /ws` — WebSocket stream mirroring PTY output bytes

Useful for debugging: watch what Claude Code is doing in the PTY session remotely.

#### Terminal Authentication

When `terminal_password` is set or xiaozhi verification is available, the terminal is protected by a login page with three auth methods:

| Method | Flow | Config |
|--------|------|--------|
| **Password** | Enter password on login page | `terminal_password: "xxx"` |
| **Voice (xiaozhi)** | Web shows code → speak to xiaozhi → xiaozhi approves | Enabled automatically when xiaozhi is connected |
| **Display (xiaozhi)** | Tell xiaozhi you want to log in → xiaozhi shows code → type into web | Enabled automatically when xiaozhi is connected |

All methods can be enabled simultaneously. The login page shows tabs for each enabled method. Voice and display verification codes are independent — they use separate code sets and different expiry times.

Xiaozhi MCP tools for terminal auth:
- `claude.prepare_voice_login()` — prepare to listen for voice verification code
- `claude.voice_approve_login(code)` — approve a voice verification code
- `claude.get_login_code()` — request a display verification code (shown on screen, not read aloud)

## MCP Tools

| Tool | Description |
|------|-------------|
| `claude.status` | Session counts, pending tasks, permission requests, PTY state |
| `claude.send_message` | Send prompt to PTY Claude — returns `task_id` immediately (async) |
| `claude.get_result` | Poll async task — returns `pending` (with preview) or `done` |
| `claude.approve` | Approve permission → sends `1\r` to PTY |
| `claude.deny` | Deny permission → sends `3\r` to PTY |
| `claude.prepare_voice_login` | Prepare to listen for voice verification code |
| `claude.voice_approve_login` | Submit voice verification code |
| `claude.get_login_code` | Request display verification code (show on screen) |

## Permission Flow

```
PTY Claude wants Bash("rm file")
  → PermissionRequest hook fires → writes /tmp/claude-xiaozhi-perms/{id}.json
  → Hook exits 0 → Claude Code shows native permission dialog in PTY
  → Xiaozhi polls claude.status → sees waiting > 0
  → User approves/denies on xiaozhi
  → claude.approve/deny → server sends keystroke to PTY dialog
```

Only PTY Claude triggers recording (via `XIAOZHI_PERMISSION_HOOK=1` env var). Interactive Claude sessions are unaffected.

## Configuration

```yaml
# config.yaml
server:
  xiaozhi_endpoint: "wss://api.xiaozhi.me/mcp/?token=..."
  reconnect_interval: 5
  env: "dev"                 # dev=auto-token if no auth, prod=require auth
  hook_port: 9999            # optional, defaults to 9999
  hook_host: "0.0.0.0"       # 0.0.0.0 for external terminal access (hooks stay local-only)
  show_terminal: true         # serve web terminal (default true)
  allow_terminal_input: true  # forward keystrokes from terminal to PTY (default true)
  terminal_token: ""           # optional, protect terminal with ?token=xxx

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
│   ├── pty_session.py         # Persistent PTY Claude subprocess
│   ├── hook_server.py         # Hook callbacks + web terminal (aiohttp)
│   ├── stop_hook.py           # Stop hook: POSTs turn content to hook server
│   ├── permission_hook.py     # PermissionRequest hook: record-only
│   ├── permission_broker.py   # File semaphore read/write
│   ├── status_monitor.py      # Session metadata polling
│   ├── mcp_tools.py           # Tool registration schemas
│   ├── protocol.py            # JSON-RPC 2.0 + MCP envelope
│   ├── transport.py           # WebSocket client (xiaozhi.me)
│   ├── config.py              # YAML config loading
│   └── web/                   # xterm.js terminal frontend
├── tests/
├── docs/
├── README.md
├── requirements.txt
└── config.yaml
```

## Requirements

- Python 3.10+
- Claude Code (for PTY session and hooks)
- xiaozhi.me account (for MCP access point token)
- See `requirements.txt` for Python dependencies

## License

MIT
