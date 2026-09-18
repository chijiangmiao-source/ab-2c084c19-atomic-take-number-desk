"""并发发号：20 个并发操作、重复提交、故障注入并发恢复。

验收要点：不同操作并发成功后，所得号码集合无重复、无缺口（恰为 1..N）。
"""
from concurrent.futures import ThreadPoolExecutor

import requests

from conftest import allocate, fresh_scene, new_op_id

WORKERS = 20


def test_concurrent_distinct_operations_no_gaps_no_duplicates(api_base):
    """20 个不同操作并发领取同一场次镜号：结果恰为 1..20。"""
    scene = fresh_scene()
    ops = [new_op_id() for _ in range(WORKERS)]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        responses = list(ex.map(lambda op: allocate(api_base, op, scene), ops))

    assert all(r.status_code == 200 for r in responses)
    numbers = sorted(r.json()["number"] for r in responses)
    assert numbers == list(range(1, WORKERS + 1))


def test_concurrent_duplicate_submissions_same_number(api_base):
    """每个操作并发提交两次（模拟超时重试/双击）：同操作同号，整体仍无重复无缺口。"""
    scene = fresh_scene()
    ops = [new_op_id() for _ in range(WORKERS)]
    tasks = [(op, i) for op in ops for i in range(2)]  # 40 个请求，20 对

    with ThreadPoolExecutor(max_workers=40) as ex:
        responses = list(ex.map(lambda t: allocate(api_base, t[0], scene), tasks))

    assert all(r.status_code == 200 for r in responses)
    by_op: dict[str, set[int]] = {}
    for (op, _), r in zip(tasks, responses):
        by_op.setdefault(op, set()).add(r.json()["number"])

    # 同一操作的两次提交拿到同一个号码
    assert all(len(v) == 1 for v in by_op.values())
    # 不同操作的号码集合恰为 1..20
    assert sorted(next(iter(v)) for v in by_op.values()) == list(range(1, WORKERS + 1))

    lst = requests.get(f"{api_base}/api/scenes/{scene}/shot-numbers").json()
    assert len(lst["items"]) == WORKERS


def test_concurrent_injected_failures_then_concurrent_retries(api_base):
    """20 个操作全部“落库后崩溃”（503），再并发重试：全部取回号码，集合 1..20，后续发号无缺口。"""
    scene = fresh_scene()
    ops = [new_op_id() for _ in range(WORKERS)]

    # 第一轮：全部注入故障 -> 全部 503（但已落库）
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        first = list(
            ex.map(lambda op: allocate(api_base, op, scene, inject_failure_after_commit=True), ops)
        )
    assert all(r.status_code == 503 for r in first)

    # 第二轮：并发重试（仍带注入标志）-> 全部取回最初号码，不再触发故障
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        second = list(
            ex.map(lambda op: allocate(api_base, op, scene, inject_failure_after_commit=True), ops)
        )
    assert all(r.status_code == 200 for r in second)
    numbers = sorted(r.json()["number"] for r in second)
    assert numbers == list(range(1, WORKERS + 1))
    assert all(r.json()["idempotent_replay"] for r in second)

    # 故障没有造成缺口：下一个新操作拿到 21
    r = allocate(api_base, new_op_id(), scene)
    assert r.json()["number"] == WORKERS + 1


def test_concurrent_mixed_load_final_set_is_contiguous(api_base):
    """混合压力：20 个操作，每个并发重复提交 3 次，其中一半首轮注入故障。"""
    scene = fresh_scene()
    ops = [new_op_id() for _ in range(WORKERS)]

    def submit(op: str, round_idx: int):
        inject = round_idx == 0 and ops.index(op) % 2 == 0
        return allocate(api_base, op, scene, inject_failure_after_commit=inject)

    tasks = [(op, i) for op in ops for i in range(3)]  # 60 个请求
    with ThreadPoolExecutor(max_workers=30) as ex:
        responses = list(ex.map(lambda t: submit(*t), tasks))

    ok_responses = [r for r in responses if r.status_code == 200]
    assert ok_responses, "expected some successful responses"
    # 所有成功响应中，同一操作的号码一致
    by_op: dict[str, set[int]] = {}
    for (op, _), r in zip(tasks, responses):
        if r.status_code == 200:
            by_op.setdefault(op, set()).add(r.json()["number"])
    # 每个操作最终都能通过重试拿到号码
    for op in ops:
        if op not in by_op:
            r = allocate(api_base, op, scene)
            assert r.status_code == 200
            by_op[op] = {r.json()["number"]}
    assert all(len(v) == 1 for v in by_op.values())
    assert sorted(next(iter(v)) for v in by_op.values()) == list(range(1, WORKERS + 1))
