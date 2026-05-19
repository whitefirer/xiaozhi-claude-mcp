import json
from xiaozhi_claude_mcp.claude_driver import ClaudeDriver, ClaudeResponse


def test_parse_json_output():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "Hello, world!",
        "session_id": "sess_001",
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    driver = ClaudeDriver(binary="echo")
    resp = driver._parse_output(raw)
    assert resp.content == "Hello, world!"
    assert resp.session_id == "sess_001"
    assert resp.tokens == 150


def test_build_command_one_shot():
    driver = ClaudeDriver(binary="claude")
    cmd = driver._build_command("What is this?", None, max_turns=5)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "What is this?" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--max-turns" in cmd
    assert "5" in cmd


def test_build_command_resume():
    driver = ClaudeDriver(binary="claude")
    cmd = driver._build_command("Continue", "sess_abc", max_turns=5)
    assert "--resume" in cmd
    assert "sess_abc" in cmd


def test_build_command_allowed_tools():
    driver = ClaudeDriver(binary="claude")
    cmd = driver._build_command("Fix bug", None, allowed_tools=["Read", "Edit", "Bash"])
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Read,Edit,Bash"


def test_parse_output_with_tool_use():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "Done.",
        "session_id": "sess_002",
        "num_turns": 3,
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 200, "output_tokens": 100},
    })
    driver = ClaudeDriver(binary="echo")
    resp = driver._parse_output(raw)
    assert resp.content == "Done."
    assert resp.session_id == "sess_002"
    assert resp.cost_usd == 0.05
