"""进程重启：服务在“落库后、回包前崩溃”，重启后同标识重试取回最初号码。"""
import os
import time

import docker
import requests

from conftest import allocate, fresh_scene, new_op_id

API_CONTAINER = os.environ.get("API_CONTAINER", "shotnum-api")


def wait_healthy(api_base: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{api_base}/api/health", timeout=2)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("API did not become healthy after restart")


def restart_api_container() -> None:
    client = docker.from_env()
    container = client.containers.get(API_CONTAINER)
    container.restart()


def test_allocation_survives_api_process_restart(api_base):
    scene = fresh_scene()
    op_a = new_op_id()

    # op A：落库后、回包前“崩溃” -> 客户端只看到 503
    r1 = allocate(api_base, op_a, scene, note="重启演练", inject_failure_after_commit=True)
    assert r1.status_code == 503

    # op B 正常拿到 2（证明 A 已落库为 1）
    r_b = allocate(api_base, new_op_id(), scene, note="正常操作")
    assert r_b.json()["number"] == 2

    # 模拟真实崩溃：重启 API 进程（数据库为独立持久化服务，不重启）
    restart_api_container()
    wait_healthy(api_base)

    # 重启后同标识重试：取回最初号码 1，不重复占号，也不再次触发故障
    r2 = allocate(api_base, op_a, scene, note="重启演练", inject_failure_after_commit=True)
    assert r2.status_code == 200
    assert r2.json()["number"] == 1
    assert r2.json()["idempotent_replay"] is True

    # 重启后新操作继续无缝发号：无重复、无缺口
    r_c = allocate(api_base, new_op_id(), scene, note="重启后的新操作")
    assert r_c.json()["number"] == 3

    lst = requests.get(f"{api_base}/api/scenes/{scene}/shot-numbers").json()
    assert [i["number"] for i in lst["items"]] == [1, 2, 3]
    assert lst["next_number"] == 4


def test_counters_and_mappings_persist_across_restart(api_base):
    """重启前分配的号码与场次计数，重启后原样保留。"""
    scene = fresh_scene()
    ops = [new_op_id() for _ in range(3)]
    expected = []
    for op in ops:
        r = allocate(api_base, op, scene, note="持久化检查")
        expected.append(r.json()["number"])
    assert expected == [1, 2, 3]

    restart_api_container()
    wait_healthy(api_base)

    lst = requests.get(f"{api_base}/api/scenes/{scene}/shot-numbers").json()
    assert [i["number"] for i in lst["items"]] == [1, 2, 3]
    assert [i["client_op_id"] for i in lst["items"]] == ops

    # 每个操作重放都取回原号码
    for op, number in zip(ops, expected):
        r = allocate(api_base, op, scene, note="持久化检查")
        assert r.json()["number"] == number
        assert r.json()["idempotent_replay"] is True
