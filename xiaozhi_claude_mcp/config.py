from dataclasses import dataclass, field
import yaml


@dataclass
class ServerConfig:
    xiaozhi_endpoint: str
    reconnect_interval: int = 5
    env: str = "dev"
    hook_port: int = 9999
    hook_host: str = "127.0.0.1"
    show_terminal: bool = True
    allow_terminal_input: bool = True
    terminal_token: str = ""
    terminal_password: str = ""
    enable_voice_auth: bool = False
    enable_display_auth: bool = False
    auth_server_url: str = ""


@dataclass
class ClaudeConfig:
    perm_dir: str = "/tmp/claude-xiaozhi-perms"


@dataclass
class StatusConfig:
    poll_interval_sec: int = 5
    exclude_paths: list[str] = field(default_factory=list)
    exclude_kinds: list[str] = field(default_factory=list)


@dataclass
class Config:
    server: ServerConfig
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    status: StatusConfig = field(default_factory=StatusConfig)


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    server = ServerConfig(**raw["server"])

    claude_raw = raw.get("claude", {})
    claude_raw.pop("binary", None)  # removed, was for old ClaudeDriver
    claude = ClaudeConfig(**claude_raw) if claude_raw else ClaudeConfig()

    status_raw = raw.get("status", {})
    status = StatusConfig(**status_raw) if status_raw else StatusConfig()

    return Config(server=server, claude=claude, status=status)
