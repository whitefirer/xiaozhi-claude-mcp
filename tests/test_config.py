import tempfile
import os
from xiaozhi_claude_mcp.config import load_config, Config

SAMPLE_YAML = """
server:
  xiaozhi_endpoint: "wss://xiaozhi.me/mcp/agent/abc123"
  reconnect_interval: 5

claude:
  perm_dir: "/tmp/claude-xiaozhi-perms"

status:
  poll_interval_sec: 5
"""


def test_load_config_from_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        path = f.name
    try:
        cfg = load_config(path)
        assert cfg.server.xiaozhi_endpoint == "wss://xiaozhi.me/mcp/agent/abc123"
        assert cfg.server.reconnect_interval == 5
        assert cfg.claude.perm_dir == "/tmp/claude-xiaozhi-perms"
        assert cfg.claude.perm_dir == "/tmp/claude-xiaozhi-perms"
        assert cfg.status.poll_interval_sec == 5
    finally:
        os.unlink(path)


def test_config_defaults():
    minimal = """
server:
  xiaozhi_endpoint: "wss://xiaozhi.me/mcp/agent/test"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(minimal)
        path = f.name
    try:
        cfg = load_config(path)
        assert cfg.server.reconnect_interval == 5
        assert cfg.claude.perm_dir == "/tmp/claude-xiaozhi-perms"
        assert cfg.claude.perm_dir == "/tmp/claude-xiaozhi-perms"
        assert cfg.status.poll_interval_sec == 5
    finally:
        os.unlink(path)
