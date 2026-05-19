from __future__ import annotations

import json
import os
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REQUEST_SUFFIX = ".json"
RESULT_SUFFIX = ".result.json"


@dataclass
class PermissionRequest:
    permission_id: str
    tool: str
    hint: str
    timestamp: float


def write_permission_request(perm_dir: str, permission_id: str, tool: str, hint: str) -> str:
    os.makedirs(perm_dir, exist_ok=True)
    data = {
        "permission_id": permission_id,
        "tool": tool,
        "hint": hint,
        "timestamp": time.time(),
    }
    path = os.path.join(perm_dir, f"{permission_id}{REQUEST_SUFFIX}")
    with open(path, "w") as f:
        json.dump(data, f)
    logger.info("Permission request written: %s", path)
    return path


def write_permission_result(perm_dir: str, permission_id: str, decision: bool) -> str:
    data = {"permission_id": permission_id, "decision": decision, "timestamp": time.time()}
    path = os.path.join(perm_dir, f"{permission_id}{RESULT_SUFFIX}")
    with open(path, "w") as f:
        json.dump(data, f)
    logger.info("Permission result written: %s = %s", permission_id, decision)
    return path


def wait_for_result(perm_dir: str, permission_id: str, poll_ms: int = 200) -> bool | None:
    result_path = os.path.join(perm_dir, f"{permission_id}{RESULT_SUFFIX}")
    deadline = time.time() + 86400
    while time.time() < deadline:
        if os.path.exists(result_path):
            try:
                with open(result_path) as f:
                    data = json.loads(f.read())
                decision = data.get("decision")
                if decision is True:
                    return True
                return False
            except (json.JSONDecodeError, KeyError):
                pass
        time.sleep(poll_ms / 1000.0)
    return None


def scan_for_requests(perm_dir: str) -> list[PermissionRequest]:
    if not os.path.exists(perm_dir):
        return []
    requests = []
    for name in os.listdir(perm_dir):
        if name.endswith(RESULT_SUFFIX):
            continue
        if name.endswith(REQUEST_SUFFIX):
            path = os.path.join(perm_dir, name)
            try:
                with open(path) as f:
                    data = json.loads(f.read())
                requests.append(PermissionRequest(
                    permission_id=data["permission_id"],
                    tool=data.get("tool", "unknown"),
                    hint=data.get("hint", ""),
                    timestamp=data.get("timestamp", 0),
                ))
            except (json.JSONDecodeError, KeyError):
                pass
    requests.sort(key=lambda r: r.timestamp)
    return requests


def cleanup_request(perm_dir: str, permission_id: str) -> None:
    for suffix in (REQUEST_SUFFIX, RESULT_SUFFIX):
        path = os.path.join(perm_dir, f"{permission_id}{suffix}")
        if os.path.exists(path):
            os.unlink(path)
