import os
import time
import uuid

import pytest
import requests

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")


def fresh_scene() -> str:
    """每个用例使用全新场次，保证号段从 1 开始且用例间互不影响。"""
    return f"scene-{uuid.uuid4()}"


def new_op_id() -> str:
    return f"op-{uuid.uuid4()}"


def allocate(
    api_base: str,
    client_op_id: str,
    scene_id: str,
    note: str = "",
    inject_failure_after_commit: bool = False,
    timeout: float = 30.0,
) -> requests.Response:
    return requests.post(
        f"{api_base}/api/shot-numbers",
        json={
            "client_op_id": client_op_id,
            "scene_id": scene_id,
            "note": note,
            "inject_failure_after_commit": inject_failure_after_commit,
        },
        timeout=timeout,
    )


@pytest.fixture(scope="session")
def api_base() -> str:
    deadline = time.time() + 90
    while True:
        try:
            r = requests.get(f"{API_BASE}/api/health", timeout=2)
            if r.status_code == 200:
                return API_BASE
        except requests.RequestException:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"API not healthy at {API_BASE}")
        time.sleep(1)
