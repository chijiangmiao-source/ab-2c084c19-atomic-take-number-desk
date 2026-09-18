"""浏览器端到端：失败保留待重试、恢复后显示唯一镜号、重复提交、冲突反馈。"""
import uuid

import requests
from playwright.sync_api import expect

from conftest import API_BASE, WEB_BASE


def fresh_scene() -> str:
    return f"E2E-{uuid.uuid4()}"


def scene_numbers(scene: str) -> list[int]:
    data = requests.get(f"{API_BASE}/api/scenes/{scene}/shot-numbers", timeout=10).json()
    return [item["number"] for item in data["items"]]


def test_failure_keeps_pending_op_and_retry_recovers_number(page):
    scene = fresh_scene()
    page.goto(f"{WEB_BASE}/?autoretry=off")

    page.get_by_test_id("scene-input").fill(scene)
    page.get_by_test_id("note-input").fill("夜戏 追车")
    page.get_by_test_id("inject-failure").check()
    page.get_by_test_id("submit-btn").click()

    # 失败反馈：横幅可见，操作保留为“待重试”，镜号未显示
    expect(page.get_by_test_id("error-banner")).to_contain_text("503")
    row = page.get_by_test_id("op-row").first
    expect(row.get_by_test_id("op-status")).to_contain_text("待重试")
    expect(row.get_by_test_id("op-number")).to_have_text("—")

    # 手动重试后恢复，显示唯一镜号
    row.get_by_test_id("retry-btn").click()
    expect(row.get_by_test_id("op-status")).to_contain_text("已确认")
    expect(row.get_by_test_id("op-number")).to_have_text("1")
    expect(page.get_by_test_id("error-banner")).to_have_count(0)

    # 刷新页面：已确认的镜号仍在（本地持久化 + 服务端落库）
    page.reload()
    expect(page.get_by_test_id("op-row").first.get_by_test_id("op-number")).to_have_text("1")

    # 服务端恰好一条记录：重试没有重复占号
    assert scene_numbers(scene) == [1]


def test_auto_retry_recovers_without_manual_action(page):
    scene = fresh_scene()
    page.goto(WEB_BASE)  # 默认开启自动重试

    page.get_by_test_id("scene-input").fill(scene)
    page.get_by_test_id("note-input").fill("雨戏")
    page.get_by_test_id("inject-failure").check()
    page.get_by_test_id("submit-btn").click()

    # 自动重试在数秒内恢复并显示镜号
    row = page.get_by_test_id("op-row").first
    expect(row.get_by_test_id("op-status")).to_contain_text("已确认", timeout=15000)
    expect(row.get_by_test_id("op-number")).to_have_text("1")
    assert scene_numbers(scene) == [1]


def test_duplicate_submission_same_op_returns_same_number(page):
    """重复提交同一操作（相同 client_op_id + 相同内容）：UI 两次都显示同一号码，服务端仅一条记录。"""
    scene = fresh_scene()
    op_id = f"e2e-{uuid.uuid4()}"
    page.goto(f"{WEB_BASE}/?autoretry=off")

    page.get_by_test_id("scene-input").fill(scene)
    page.get_by_test_id("note-input").fill("日戏")
    page.get_by_test_id("op-id-override").fill(op_id)
    page.get_by_test_id("submit-btn").click()

    first_row = page.get_by_test_id("op-row").first
    expect(first_row.get_by_test_id("op-status")).to_contain_text("已确认")
    expect(first_row.get_by_test_id("op-number")).to_have_text("1")

    # 再次提交完全相同的负载（相同标识 + 相同内容）-> 幂等回放同一号码
    page.get_by_test_id("note-input").fill("日戏")
    page.get_by_test_id("submit-btn").click()
    expect(page.get_by_test_id("op-row")).to_have_count(2)
    second_row = page.get_by_test_id("op-row").first
    expect(second_row.get_by_test_id("op-status")).to_contain_text("已确认")
    expect(second_row.get_by_test_id("op-number")).to_have_text("1")

    assert scene_numbers(scene) == [1]


def test_conflicting_payload_shows_409_feedback(page):
    """同一 client_op_id 换内容：页面展示 409 冲突反馈，服务端记录不变。"""
    scene = fresh_scene()
    op_id = f"e2e-{uuid.uuid4()}"
    page.goto(f"{WEB_BASE}/?autoretry=off")

    page.get_by_test_id("scene-input").fill(scene)
    page.get_by_test_id("note-input").fill("版本A")
    page.get_by_test_id("op-id-override").fill(op_id)
    page.get_by_test_id("submit-btn").click()
    expect(page.get_by_test_id("op-row").first.get_by_test_id("op-status")).to_contain_text("已确认")

    # 相同标识、不同内容 -> 409 冲突反馈
    page.get_by_test_id("note-input").fill("版本B")
    page.get_by_test_id("submit-btn").click()
    expect(page.get_by_test_id("error-banner")).to_contain_text("409")
    conflict_row = page.get_by_test_id("op-row").filter(has_text="版本B")
    expect(conflict_row.get_by_test_id("op-status")).to_contain_text("内容冲突")
    expect(conflict_row.get_by_test_id("op-number")).to_have_text("—")

    # 冲突未消耗号码：服务端仍只有最初一条记录
    assert scene_numbers(scene) == [1]


def test_multiple_operations_get_unique_sequential_numbers(page):
    """页面连续领取：镜号从 1 开始严格连续。"""
    scene = fresh_scene()
    page.goto(WEB_BASE)

    page.get_by_test_id("scene-input").fill(scene)
    for i in range(1, 4):
        page.get_by_test_id("note-input").fill(f"镜头 {i}")
        page.get_by_test_id("submit-btn").click()
        expect(
            page.get_by_test_id("op-row").first.get_by_test_id("op-number")
        ).to_have_text(str(i))

    assert scene_numbers(scene) == [1, 2, 3]
