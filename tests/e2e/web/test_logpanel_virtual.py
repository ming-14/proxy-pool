"""LogPanel 悬浮形态 e2e（真实浏览器 + 合成日志源）。

为什么用探针而不是真实 serve：虚拟列表的缺陷只在「行数远多于视口、行高可变」时暴露，
后端真实日志在测试里灌不到几千条。探针页以组件源码 + 固定种子合成数据驱动，
断言全部基于可见行为（滚动几何、渲染行、状态文本、按钮 aria-pressed），不依赖组件内部字段。

覆盖的不变式（桌面视口，形态为悬浮窗）：
- 全程滚动不出现空白（顶部/底部露白）
- 滚动过程中总高与滚动位置无关（滚动条滑块按 scrollTop/scrollHeight 映射，总高一变就与鼠标失步）
- 环形缓冲淘汰时，锚定行保持在视口原位
- 跟随模式下始终贴底，且 DOM 行数远小于条目数（虚拟化生效）
- 手动关闭跟随后，新日志不得被程序侧滚动顶回跟随
- 暂停 → 继续后，视图补上暂停期间到达的日志
- 搜索/等级/模块过滤改变渲染集，命中文本高亮；正则非法标红并降级；清空后归零
- 展开态按条目 key 记忆：行被回收重建后仍保持展开
- 隐藏 → 显示：跟随态贴回底部、非跟随态保留滚动位置
- 标题栏右键菜单可开合，点菜单项关闭面板
- 拖拽缩放后重测行高，滚动区不出现空白

抽屉形态的用例见 test_logpanel_sheet.py。
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.e2e.web.logpanel_probe import (  # noqa: E402
    GAP_TOL,
    assert_no_gap,
    open_probe,
)

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}


@pytest.fixture()
def probe(probe_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context, driver, errors = open_probe(browser, probe_server, viewport=DESKTOP_VIEWPORT)
        try:
            # 护栏：本文件的用例全部按悬浮形态构造，断点调整后不该静默跑成抽屉
            assert driver.form() == "floating", "桌面视口应落在悬浮形态"
            yield driver, errors
        finally:
            context.close()
            browser.close()


def test_scrolling_full_range_has_no_gap(probe):
    """全程滚动：视口内不得出现顶部/底部空白"""
    p, errors = probe
    p.fill(1500)
    p.click_auto_scroll()  # 关跟随，避免被强制贴底
    p.wait(300)
    state = p.state()
    assert state["follow"] is False, "自动滚动未关闭"

    max_pos = int(state["scrollHeight"] - state["clientHeight"])
    assert max_pos > 0, "内容未铺满滚动区（无法验证滚动）"
    for pos in range(0, max_pos, 500):
        p.scroll_to(pos)
        p.wait(150)
        assert_no_gap(p.state(), f"滚动到 {pos}px")
    assert not errors


def test_scroll_height_stable_while_scrolling(probe):
    """总高不得随滚动位置变化：原生滚动条滑块按 scrollTop/scrollHeight 映射，
    总高被滚动过程中的行高修正改变时，滑块就与鼠标脱节（拖动滚动条跟不上鼠标）"""
    p, errors = probe
    p.fill(1500)
    p.click_auto_scroll()  # 关跟随，避免贴底掩盖滚动
    p.wait(300)
    state = p.state()
    assert state["follow"] is False, "自动滚动未关闭"
    max_pos = int(state["scrollHeight"] - state["clientHeight"])
    assert max_pos > 0, "内容未铺满滚动区（无法验证滚动）"

    heights = set()
    for pos in range(0, max_pos, 2000):
        p.scroll_to(pos)
        p.wait(120)
        state = p.state()
        assert state["scrollTop"] == pos, f"滚动位置被程序改写：{pos} → {state['scrollTop']}"
        heights.add(state["scrollHeight"])
    assert len(heights) == 1, f"滚动过程中总高发生变化（滚动条会与鼠标失步）：{sorted(heights)}"
    assert not errors


def test_trim_compensation_keeps_anchor_line(probe):
    """缓冲满后继续推送：未被淘汰的锚定行必须留在视口原位"""
    p, errors = probe
    p.fill(1500)
    p.fill(600)  # 灌到容量上限 2000，之后每条新日志都淘汰一条旧的
    p.scroll_to(20000)
    p.wait(500)
    before_text = p.line_text_at(10)
    before_top = p.line_top(10)
    assert before_text and before_top is not None

    p.push(100)
    p.wait(800)
    after_text = p.line_text_at(10)
    after_top = p.line_top(10)
    assert after_text == before_text, "锚定行被换掉了"
    assert abs(after_top - before_top) <= 1, f"锚定行漂移 {after_top - before_top}px"
    assert not errors


def test_follow_mode_sticks_to_bottom(probe):
    """跟随模式：始终贴底，且 DOM 行数远小于条目数（虚拟化生效）"""
    p, errors = probe
    p.fill(500)
    assert p.state()["follow"] is True, "默认应处于跟随态"

    for _ in range(10):
        p.push(50)
        p.wait(120)
    p.wait(600)
    assert p.state()["follow"] is True, "持续追加日志后应仍处于跟随态"

    p.wait_at_bottom()
    state = p.state()
    assert state["distanceFromEnd"] <= GAP_TOL, f"跟随模式未贴底：距底 {state['distanceFromEnd']}px"
    assert_no_gap(state, "跟随模式")
    assert state["domLines"] < state["shown"] / 5, f"DOM 行数 {state['domLines']} 未虚拟化（共 {state['shown']} 条）"
    assert not errors


def test_manual_follow_off_is_not_reverted(probe):
    """手动关闭跟随后，停在底部也不得被新日志重新开启"""
    p, errors = probe
    p.fill(300)
    p.click_auto_scroll()
    p.wait(200)
    before = p.state()
    assert before["follow"] is False, "自动滚动未关闭"
    assert before["distanceFromEnd"] <= GAP_TOL, "前置条件：视口应停在底部"

    p.push(5)
    p.wait(600)
    after = p.state()
    assert after["follow"] is False, "手动关闭的跟随后被程序滚动重新开启"
    assert after["scrollTop"] == before["scrollTop"], f"视口被强制贴底（{before['scrollTop']} → {after['scrollTop']}）"
    assert after["distanceFromEnd"] > GAP_TOL, "新日志应留在视口下方"
    assert not errors


def test_resume_after_pause_catches_up(probe):
    """暂停期间到达的日志，继续后必须补进视图（不能停在暂停那一刻）"""
    p, errors = probe
    p.fill(300)
    p.click_pause()
    p.wait(150)
    assert p.state()["paused"] is True
    before = p.state()["shown"]

    p.push(300)
    p.wait(600)
    assert p.state()["shown"] == before, "暂停期间视图不应变化"

    p.click_pause()
    p.wait(800)
    after = p.state()
    assert after["paused"] is False
    assert after["shown"] == before + 300, f"继续后视图未补上暂停期间的日志：{before} → {after['shown']}"
    assert not errors


def test_filters_and_highlight(probe):
    """搜索/等级/模块过滤改变渲染集，命中文本高亮为 mark"""
    p, errors = probe
    p.fill(800)
    total = p.state()["shown"]

    p.set_query("校验通过")
    p.wait(400)
    searched = p.state()
    assert 0 < searched["shown"] < total, f"搜索未生效：{searched['shown']} / {total}"
    assert searched["domLines"] > 0, "搜索命中后应有渲染行"
    marks = p.run("() => document.querySelectorAll('[data-testid=logpanel] .lp-row .lp-text mark').length")
    assert marks > 0, "命中文本未高亮"
    assert_no_gap(searched, "过滤后")

    p.set_query("")
    p.wait(400)
    assert p.state()["shown"] == total, "清空搜索未恢复全部条目"

    p.toggle_level(1)  # 关掉 INFO
    p.wait(400)
    after_level = p.state()["shown"]
    assert 0 < after_level < total, f"等级过滤未生效：{after_level} / {total}"

    p.toggle_tag("scheduler")
    p.wait(400)
    assert p.state()["shown"] < after_level, "模块过滤未生效"
    assert not errors


def test_expanded_state_survives_row_recycling(probe):
    """展开态按条目 key 记忆：行滚出视口被回收、滚回来仍是展开的"""
    p, errors = probe
    p.fill(600)
    p.click_auto_scroll()  # 关跟随，便于固定位置
    p.wait(300)
    p.scroll_to(2000)
    p.wait(300)

    row = p.first_expandable()
    assert row >= 0, "前置条件：视口内应有可展开行（带堆栈）"
    text = p.line_text_at(row)
    assert p.expanded_texts() == [], "前置条件：初始应无展开行"

    p.click_line(row)
    p.wait(300)
    assert text in p.expanded_texts(), "点击后未展开"

    # 滚到远处让该行被回收（此时不在 DOM 里），再滚回来
    p.scroll_to(12000)
    p.wait(400)
    assert p.expanded_texts() == [], "该行滚出视口后仍留在 DOM 中（未回收）"
    p.scroll_to(2000)
    p.wait(400)

    assert text in p.expanded_texts(), "行被回收重建后展开态丢失"
    assert_no_gap(p.state(), "展开行重建后")
    assert not errors


def test_hide_show_keeps_scroll_position(probe):
    """隐藏 → 显示：跟随态贴回底部，非跟随态滚动位置原样保留"""
    p, errors = probe
    p.fill(800)

    # 跟随态（默认）：隐藏期间到达的日志，显示后应贴回底部
    p.push(50)
    p.hide()
    p.wait(500)
    p.push(50)
    p.show()
    p.wait(1000)
    assert p.state()["distanceFromEnd"] <= GAP_TOL, "跟随态重新显示后未贴底"

    # 非跟随态：滚动位置与总高原样保留
    p.click_auto_scroll()
    p.wait(200)
    p.scroll_to(3000)
    p.wait(400)
    before = p.state()

    p.hide()
    p.wait(500)
    assert p.state()["shown"] == before["shown"], "隐藏期间条目数不应变化"

    p.show()
    p.wait(1000)
    after = p.state()
    assert after["scrollTop"] == before["scrollTop"], f"滚动位置未保留：{before['scrollTop']} → {after['scrollTop']}"
    assert after["scrollHeight"] == before["scrollHeight"], "总高变化（行高被损坏）"
    assert_no_gap(after, "重新显示后")
    assert not errors


def test_regex_mode_and_clear(probe):
    """正则模式：非法表达式标红并降级为字面量，合法表达式参与过滤；清空后缓冲与视图归零"""
    p, errors = probe
    p.fill(300)
    total = p.state()["shown"]
    assert total > 0

    p.toggle_regex()
    p.set_query("([)")
    p.wait(300)
    assert p.run("() => document.querySelector('.lp-search-invalid') !== null") is True, "非法正则未标红"
    assert p.state()["shown"] == 0, "非法正则应按字面量匹配（无命中）"

    p.set_query("^心跳")
    p.wait(300)
    matched = p.state()["shown"]
    assert 0 < matched < total, f"正则过滤未生效：{matched} / {total}"

    p.click_clear()
    p.wait(400)
    state = p.state()
    assert state["shown"] == 0 and state["buffered"] == 0, f"清空后仍有数据：{state['shown']} / {state['buffered']}"
    assert state["domLines"] == 0, "清空后仍渲染日志行"
    assert not errors


def test_context_menu_opens_and_closes_panel(probe):
    """标题栏右键：菜单在点击处出现，点菜单项关闭面板"""
    p, errors = probe
    p.fill(50)
    assert p.menu_visible() is False, "初始不应有菜单"

    p.open_menu()
    p.wait(200)
    assert p.menu_visible() is True, "右键后菜单未出现"

    p.click_menu_close()
    p.wait(200)
    assert p.menu_visible() is False, "点菜单项后菜单未关闭"
    assert p.run("() => window.__probe.state()['domLines']") == 0, "关闭面板后仍渲染日志行"
    assert not errors


def test_drag_moves_floating_window(probe):
    """悬浮态拖拽标题栏移动窗口，并持久化几何"""
    p, errors = probe
    p.fill(50)
    before = p.root_box()
    assert before is not None

    p.drag_title(60)
    p.wait(300)
    after = p.root_box()
    assert after["y"] > before["y"], "拖拽标题栏未移动窗口"
    assert abs((after["y"] - before["y"]) - 60) <= 2, f"位移不符：{after['y'] - before['y']}px"
    assert p.geom_persisted() is not None, "拖拽后几何未持久化"
    assert not errors


def test_resize_remeasures_rows(probe):
    """拖拽缩放后重测行高，滚动区不出现空白"""
    p, errors = probe
    p.fill(600)
    p.click_auto_scroll()  # 关跟随，避免贴底掩盖问题
    p.wait(200)

    handle = p.page.locator('[data-testid="lp-resize"]').bounding_box()
    assert handle, "未找到缩放手柄"
    p.page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    p.page.mouse.down()
    p.page.mouse.move(handle["x"] - 200, handle["y"] - 60, steps=5)
    p.page.mouse.up()
    p.wait(1000)

    state = p.state()
    assert state["domLines"] > 0, "缩放后无渲染行"
    assert_no_gap(state, "缩放后")
    assert not errors


def test_resize_keeps_follow_at_bottom(probe):
    """跟随态下把窗口拖高（可视高度变大、内容高度不变）后仍要贴底：
    此时 scrollTop 不变而 clientHeight 变大，末行会落到视口下方"""
    p, errors = probe
    p.fill(400)
    p.wait(400)
    assert p.state()["follow"] is True, "前置条件：应处于跟随态"

    handle = p.page.locator('[data-testid="lp-resize"]').bounding_box()
    assert handle, "未找到缩放手柄"
    p.page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    p.page.mouse.down()
    p.page.mouse.move(handle["x"], handle["y"] - 120, steps=5)
    p.page.mouse.up()
    p.wait(1000)

    state = p.state()
    assert state["distanceFromEnd"] <= GAP_TOL, f"拖高后未贴底：距底 {state['distanceFromEnd']}px"
    assert_no_gap(state, "跟随态拖高后")
    assert not errors
