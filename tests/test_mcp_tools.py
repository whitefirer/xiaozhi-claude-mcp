from xiaozhi_claude_mcp.mcp_tools import (
    get_tools_list,
    make_text_content,
)


def test_tools_list_has_all_six_tools():
    tools = get_tools_list()
    names = [t["name"] for t in tools]
    assert "claude.status" in names
    assert "claude.send_message" in names
    assert "claude.approve" in names
    assert "claude.deny" in names
    assert "claude.notify_permission" in names
    assert "claude.notify_turn" in names


def test_tool_schemas_have_required_fields():
    for tool in get_tools_list():
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert "type" in tool["inputSchema"]


def test_claude_status_schema():
    status = next(t for t in get_tools_list() if t["name"] == "claude.status")
    schema = status["inputSchema"]
    assert schema["type"] == "object"
    assert "properties" in schema


def test_claude_send_message_schema():
    sm = next(t for t in get_tools_list() if t["name"] == "claude.send_message")
    props = sm["inputSchema"]["properties"]
    assert "prompt" in props
    assert props["prompt"]["type"] == "string"
    assert "session_id" in props


def test_claude_approve_deny_schemas():
    approve = next(t for t in get_tools_list() if t["name"] == "claude.approve")
    assert "permission_id" in approve["inputSchema"]["properties"]

    deny = next(t for t in get_tools_list() if t["name"] == "claude.deny")
    assert "permission_id" in deny["inputSchema"]["properties"]


def test_make_text_content():
    result = make_text_content("hello world")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "hello world"
