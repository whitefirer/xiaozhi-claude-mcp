import json
import os
import tempfile
from xiaozhi_claude_mcp.permission_broker import (
    write_permission_request,
    wait_for_result,
    write_permission_result,
    cleanup_request,
    scan_for_requests,
)


def test_write_and_read_permission_request():
    perm_dir = tempfile.mkdtemp()
    try:
        path = write_permission_request(
            perm_dir, "sess_test", "bash", "rm -rf /tmp/*"
        )
        assert os.path.exists(path)
        with open(path) as f:
            data = json.loads(f.read())
        assert data["permission_id"] == "sess_test"
        assert data["tool"] == "bash"
        assert data["hint"] == "rm -rf /tmp/*"
        assert data["timestamp"] > 0
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_write_permission_result():
    perm_dir = tempfile.mkdtemp()
    try:
        write_permission_result(perm_dir, "sess_test", True)
        res_path = os.path.join(perm_dir, "sess_test.result.json")
        assert os.path.exists(res_path)
        with open(res_path) as f:
            data = json.loads(f.read())
        assert data["permission_id"] == "sess_test"
        assert data["decision"] is True
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_write_permission_result_deny():
    perm_dir = tempfile.mkdtemp()
    try:
        write_permission_result(perm_dir, "sess_test", False)
        res_path = os.path.join(perm_dir, "sess_test.result.json")
        with open(res_path) as f:
            data = json.loads(f.read())
        assert data["decision"] is False
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_cleanup_request():
    perm_dir = tempfile.mkdtemp()
    try:
        path = write_permission_request(perm_dir, "sess_clean", "Write", "edit file")
        assert os.path.exists(path)
        cleanup_request(perm_dir, "sess_clean")
        assert not os.path.exists(path)
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_scan_for_requests():
    perm_dir = tempfile.mkdtemp()
    try:
        write_permission_request(perm_dir, "sess_a", "bash", "cmd a")
        write_permission_request(perm_dir, "sess_b", "Write", "cmd b")
        found = scan_for_requests(perm_dir)
        assert len(found) == 2
        assert any(r.permission_id == "sess_a" for r in found)
        assert any(r.permission_id == "sess_b" for r in found)
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)


def test_hook_wait_for_result_allow(tmp_path):
    perm_dir = str(tmp_path)
    write_permission_request(perm_dir, "hook_test", "bash", "ls")
    write_permission_result(perm_dir, "hook_test", True)
    result = wait_for_result(perm_dir, "hook_test", poll_ms=10)
    assert result is True


def test_hook_wait_for_result_deny(tmp_path):
    perm_dir = str(tmp_path)
    write_permission_request(perm_dir, "hook_test_2", "Write", "edit")
    write_permission_result(perm_dir, "hook_test_2", False)
    result = wait_for_result(perm_dir, "hook_test_2", poll_ms=10)
    assert result is False


def test_scan_empty_dir():
    perm_dir = tempfile.mkdtemp()
    try:
        found = scan_for_requests(perm_dir)
        assert found == []
    finally:
        import shutil
        shutil.rmtree(perm_dir, ignore_errors=True)
