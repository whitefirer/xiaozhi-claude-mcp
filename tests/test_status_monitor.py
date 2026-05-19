import json
import tempfile
from xiaozhi_claude_mcp.status_monitor import StatusMonitor, Heartbeat
from xiaozhi_claude_mcp.permission_broker import write_permission_request


def test_heartbeat_defaults():
    hb = Heartbeat()
    assert hb.total == 0
    assert hb.running == 0
    assert hb.waiting == 0
    assert hb.tokens == 0


def test_parse_claude_json_output():
    monitor = StatusMonitor(claude_binary="echo")
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "Analysis complete.",
        "session_id": "sess_001",
        "num_turns": 3,
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 200, "output_tokens": 100},
    })
    hb = monitor._build_heartbeat(raw)
    assert "Analysis complete" in hb.msg
    assert hb.tokens == 300


def test_get_heartbeat_with_pending_permissions():
    perm_dir = tempfile.mkdtemp()
    try:
        write_permission_request(perm_dir, "req_001", "bash", "rm file")
        write_permission_request(perm_dir, "req_002", "Write", "edit")

        from xiaozhi_claude_mcp.permission_broker import scan_for_requests
        pending = scan_for_requests(perm_dir)

        monitor = StatusMonitor()
        hb = monitor.get_heartbeat(pending)
        assert hb.waiting == 2
        assert hb.prompt is not None
        assert hb.prompt["id"] == "req_001"
        assert hb.prompt["tool"] == "bash"
        assert hb.prompt["hint"] == "rm file"
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_heartbeat_to_dict():
    hb = Heartbeat(
        total=3,
        running=1,
        waiting=1,
        msg="Working...",
        entries=["line1", "line2"],
        tokens=5000,
        tokens_today=1000,
        prompt={"id": "req_abc", "tool": "bash", "hint": "cmd"},
    )
    d = hb.to_dict()
    assert d["total"] == 3
    assert d["running"] == 1
    assert d["waiting"] == 1
    assert d["prompt"]["id"] == "req_abc"


def test_heartbeat_to_dict_without_prompt():
    hb = Heartbeat(total=1, running=1, waiting=0, msg="ok")
    d = hb.to_dict()
    assert "prompt" not in d


def test_get_heartbeat_no_permissions():
    monitor = StatusMonitor()
    hb = monitor.get_heartbeat(None)
    assert hb.waiting == 0
    assert hb.prompt is None


def test_build_heartbeat_invalid_json():
    monitor = StatusMonitor()
    hb = monitor._build_heartbeat("not json")
    assert hb.tokens == 0
    assert hb.msg == ""
