"""发号基本语义：连续、幂等、冲突、故障注入。"""
import uuid

import requests

from conftest import allocate, fresh_scene, new_op_id


def test_sequential_numbers_start_at_one(api_base):
    scene = fresh_scene()
    numbers = [
        allocate(api_base, new_op_id(), scene).json()["number"] for _ in range(3)
    ]
    assert numbers == [1, 2, 3]


def test_same_op_same_content_replays_original_number(api_base):
    scene = fresh_scene()
    op = new_op_id()
    r1 = allocate(api_base, op, scene, note="雨夜")
    assert r1.status_code == 200
    assert r1.json()["idempotent_replay"] is False
    number = r1.json()["number"]

    # 超时重试 / 重复提交：相同标识 + 相同内容 -> 始终取回最初号码
    for _ in range(3):
        r = allocate(api_base, op, scene, note="雨夜")
        assert r.status_code == 200
        assert r.json()["number"] == number
        assert r.json()["idempotent_replay"] is True

    # 服务端只有一条记录，重试不会重复占号
    lst = requests.get(f"{api_base}/api/scenes/{scene}/shot-numbers").json()
    assert len(lst["items"]) == 1
    assert lst["next_number"] == number + 1


def test_same_op_different_content_returns_409(api_base):
    scene = fresh_scene()
    op = new_op_id()
    r1 = allocate(api_base, op, scene, note="备注A")
    assert r1.status_code == 200

    # 同一标识换备注 -> 409
    r2 = allocate(api_base, op, scene, note="备注B")
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "client_op_id_conflict"

    # 同一标识换场次 -> 409
    r3 = allocate(api_base, op, fresh_scene(), note="备注A")
    assert r3.status_code == 409

    # 冲突不消耗号码：下一个新操作仍拿到连续号码
    r4 = allocate(api_base, new_op_id(), scene)
    assert r4.json()["number"] == 2


def test_injected_failure_commits_then_retry_returns_same_number(api_base):
    scene = fresh_scene()
    op = new_op_id()

    # 首次：落库后、回包前“崩溃” -> 客户端看到 503
    r1 = allocate(api_base, op, scene, note="爆破戏", inject_failure_after_commit=True)
    assert r1.status_code == 503

    # 号码其实已落库：另一个操作拿到 2，证明故障操作占了 1 且无缺口
    r_other = allocate(api_base, new_op_id(), scene)
    assert r_other.json()["number"] == 2

    # 同标识重试：取回最初号码 1，且不再触发故障（即使仍带注入标志）
    r2 = allocate(api_base, op, scene, note="爆破戏", inject_failure_after_commit=True)
    assert r2.status_code == 200
    assert r2.json()["number"] == 1
    assert r2.json()["idempotent_replay"] is True

    # 故障 + 重试后号段无重复无缺口
    lst = requests.get(f"{api_base}/api/scenes/{scene}/shot-numbers").json()
    assert [i["number"] for i in lst["items"]] == [1, 2]


def test_failure_injection_flag_not_persisted_as_content(api_base):
    """注入标志是传输级元数据，不参与内容比较：重试时不带标志也算相同内容。"""
    scene = fresh_scene()
    op = new_op_id()
    r1 = allocate(api_base, op, scene, note="x", inject_failure_after_commit=True)
    assert r1.status_code == 503
    r2 = allocate(api_base, op, scene, note="x", inject_failure_after_commit=False)
    assert r2.status_code == 200
    assert r2.json()["number"] == 1


def test_invalid_request_rejected(api_base):
    r = requests.post(
        f"{api_base}/api/shot-numbers",
        json={"client_op_id": "", "scene_id": "s", "note": ""},
        timeout=10,
    )
    assert r.status_code == 422
    r = requests.post(
        f"{api_base}/api/shot-numbers",
        json={"client_op_id": f"op-{uuid.uuid4()}", "scene_id": "", "note": ""},
        timeout=10,
    )
    assert r.status_code == 422
