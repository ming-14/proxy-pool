"""LogPanel 抽屉形态 e2e（真实浏览器 + 移动端模拟上下文 + 合成日志源）。

覆盖的不变式（窄屏/触屏）：
- 形态跟随视口宽度（桌面悬浮 ↔ 窄屏抽屉），切换后内联样式不残留上一形态的键
- 抽屉满宽贴底、不横向溢出
- 档位切换（按钮点按 / 标题栏点按 / 拖拽 / 快滑）后仍贴底且无空白
- 触屏长按标题栏打开菜单，且不改动档位
- 安全区垫底、搜索框字号（防 iOS 聚焦缩放）
- 宽度变化后按新宽度重测行高（无空白、总高变化）
- 视口变矮（模拟软键盘/地址栏）后抽屉高度跟随
- 模块筛选条开合与横向布局
- 抽屉会话不写悬浮几何（两套尺寸互不污染）

软键盘「只改可视视口」的路径在无头 Chromium 里不可复现（visualViewport 恒等于布局视口），
键盘内缩量的正确性由单测覆盖（frontend/tests/unit/logpanel-sheet.test.ts），
这里覆盖「视口整体变矮」这一等价集成路径。

悬浮形态的用例见 test_logpanel_virtual.py。
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.e2e.web.logpanel_probe import (  # noqa: E402
    DETENT_SETTLE_MS,
    GAP_TOL,
    assert_no_gap,
    open_probe,
)

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
# 抽屉半屏档 = 可视高度 × 0.55，全屏档 = 可视高度
MOBILE_HALF_HEIGHT = round(MOBILE_VIEWPORT["height"] * 0.55)


@pytest.fixture()
def probe_mobile(probe_server):
    """移动端模拟上下文（触屏 + 窄屏）：形态自动落在抽屉"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context, driver, errors = open_probe(
            browser,
            probe_server,
            viewport=MOBILE_VIEWPORT,
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
        )
        try:
            # 护栏：触控轴依赖 (pointer: coarse)，模拟不生效时用例会以假绿的方式通过
            coarse = driver.run("() => matchMedia('(pointer: coarse)').matches")
            assert coarse is True, "移动端模拟未生效（pointer: coarse 未命中）"
            assert driver.form() == "sheet", "窄屏应自动落在抽屉形态"
            yield driver, errors
        finally:
            context.close()
            browser.close()


@pytest.fixture()
def probe_desktop(probe_server):
    """桌面上下文：用于形态随宽度切换、宽度变化重测等需要跨形态的用例"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context, driver, errors = open_probe(browser, probe_server, viewport=DESKTOP_VIEWPORT)
        try:
            assert driver.form() == "floating", "桌面视口应落在悬浮形态"
            yield driver, errors
        finally:
            context.close()
            browser.close()


def test_form_follows_viewport_width(probe_desktop):
    """形态随视口宽度切换，且内联样式不残留上一形态的键"""
    p, errors = probe_desktop
    p.fill(50)

    floating_keys = p.inline_style_keys()
    assert {"left", "top", "width", "height"} <= set(floating_keys), f"悬浮形态缺几何键：{floating_keys}"

    p.page.set_viewport_size(MOBILE_VIEWPORT)
    p.wait(400)
    assert p.form() == "sheet", "窄屏未切到抽屉形态"
    sheet_keys = p.inline_style_keys()
    assert "bottom" in sheet_keys, f"抽屉形态缺 bottom 键：{sheet_keys}"
    assert not {"left", "top", "width"} & set(sheet_keys), f"抽屉形态残留悬浮几何键：{sheet_keys}"

    p.page.set_viewport_size(DESKTOP_VIEWPORT)
    p.wait(400)
    assert p.form() == "floating", "回到宽视口未切回悬浮形态"
    restored_keys = p.inline_style_keys()
    assert {"left", "top", "width", "height"} <= set(restored_keys), f"回到悬浮后几何键未恢复：{restored_keys}"
    assert "bottom" not in restored_keys, f"回到悬浮后残留抽屉的 bottom 键：{restored_keys}"
    assert not errors


def test_sheet_fills_width_and_sticks_to_bottom(probe_mobile):
    """抽屉满宽贴底、不横向溢出，列表有可视高度"""
    p, errors = probe_mobile
    p.fill(200)
    box = p.root_box()
    assert box is not None
    assert abs(box["x"]) <= 1, f"抽屉未贴左边缘：x={box['x']}"
    assert abs(box["w"] - MOBILE_VIEWPORT["width"]) <= 1, f"抽屉未满宽：w={box['w']}"
    assert abs(box["bottom"] - MOBILE_VIEWPORT["height"]) <= 1, f"抽屉未贴底：bottom={box['bottom']}"

    overflow = p.run("() => document.documentElement.scrollWidth")
    assert overflow <= MOBILE_VIEWPORT["width"], f"页面横向溢出：{overflow}px"
    assert p.list_client_height() > 0, "抽屉内列表无可视高度"
    assert not errors


def test_detent_toggle_keeps_follow_at_bottom(probe_mobile):
    """按钮点按与标题栏点按都能切档，切档后仍贴底且无空白"""
    p, errors = probe_mobile
    p.fill(600)
    p.wait(400)
    assert p.detent() == "half", "初始应为半屏档"
    assert p.state()["follow"] is True, "前置条件：应处于跟随态"

    p.click_detent()
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "full", "点按钮未切到全屏档"
    assert abs(p.root_box()["h"] - MOBILE_VIEWPORT["height"]) <= 1, "全屏档未铺满可视高度"
    p.wait_at_bottom()
    state = p.state()
    assert state["distanceFromEnd"] <= GAP_TOL, f"全屏档未贴底：距底 {state['distanceFromEnd']}px"
    assert_no_gap(state, "全屏档")

    p.tap_title()
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "half", "点按标题栏未切回半屏档"
    assert abs(p.root_box()["h"] - MOBILE_HALF_HEIGHT) <= 1, f"半屏档高度不符：{p.root_box()['h']}"
    p.wait_at_bottom()
    state = p.state()
    assert state["distanceFromEnd"] <= GAP_TOL, f"半屏档未贴底：距底 {state['distanceFromEnd']}px"
    assert_no_gap(state, "半屏档")
    assert not errors


def test_drag_switches_detent(probe_mobile):
    """拖拽切档：位移过半即翻档，位移不足保持原档，快滑按方向翻档"""
    p, errors = probe_mobile
    p.fill(100)

    p.drag_title(-200)
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "full", "上拖未展开到全屏档"

    p.drag_title(-40)
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "full", "位移不足时不应翻档"

    p.drag_title(260)
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "half", "下拖未收起到半屏档"

    p.drag_title(-150, duration_ms=50)  # 快滑：位移与速度都过阈值
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "full", "快速上滑未翻档"
    assert not errors


def test_detent_settle_starts_from_finger_position(probe_mobile):
    """松手后必须接着手指所在的位置滑向目标档位：不得先瞬移回原档位再播放动画"""
    p, errors = probe_mobile
    p.fill(100)
    assert p.detent() == "half", "初始应为半屏档"
    origin_top = p.root_box()["y"]

    p.drag_title_hold(-300)
    p.wait(50)
    held = p.root_box()
    assert abs(held["y"] - (origin_top - 300)) <= 2, f"拖拽中未跟随手指：{origin_top} → {held['y']}"

    p.release_title()
    settled = p.root_box()
    assert abs(settled["y"] - held["y"]) <= 120, (
        f"松手瞬间瞬移回原档位（拖到 {held['y']}，松手后 {settled['y']}）"
    )

    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "full", "松手后应定在全屏档"
    assert abs(p.root_box()["y"]) <= 1, "松手后未滑到全屏档位置"
    assert not errors


def test_long_press_opens_menu_without_changing_detent(probe_mobile):
    """触屏长按标题栏打开菜单，且不改动档位（长按抬手不得触发点按切档）"""
    p, errors = probe_mobile
    p.fill(50)
    assert p.menu_visible() is False, "初始不应有菜单"
    before = p.detent()

    p.long_press_title()
    p.wait(200)
    assert p.menu_visible() is True, "长按未打开菜单"
    assert p.detent() == before, "长按抬手把档位切了（合成 click 未抑制）"

    p.click_menu_close()
    p.wait(200)
    assert p.menu_visible() is False, "点菜单项后菜单未关闭"
    assert p.run("() => window.__probe.state()['domLines']") == 0, "关闭面板后仍渲染日志行"
    assert not errors


def test_safe_area_keeps_content_off_bottom_edge(probe_mobile):
    """安全区垫底：列表底边与面板底边之间留出安全区高度"""
    p, errors = probe_mobile
    p.fill(100)
    # env() 在无刘海模拟下恒为 0，只能注入解析层变量验证规则本身
    p.page.add_style_tag(content='[data-testid="logpanel"] { --lp-safe-bottom: 34px; }')
    p.wait(200)
    gap = p.run(
        """() => {
          const root = document.querySelector('[data-testid="logpanel"]');
          const list = root.querySelector('.lp-list');
          return Math.round(root.getBoundingClientRect().bottom - list.getBoundingClientRect().bottom);
        }"""
    )
    assert abs(gap - 34) <= 1, f"列表未让开安全区：底边距 {gap}px"
    assert not errors


def test_search_input_font_size_avoids_ios_zoom(probe_mobile):
    """抽屉态搜索框字号不小于 16px：iOS 聚焦更小字号会把整页放大并抖动形态"""
    p, errors = probe_mobile
    size = p.run(
        "() => getComputedStyle(document.querySelector('[data-testid=lp-search]')).fontSize"
    )
    assert size == "16px", f"搜索框字号 {size}，iOS 聚焦会整页缩放"
    assert not errors


def test_width_change_remeasures_rows(probe_desktop):
    """形态切换改变行宽：必须按新宽度重测行高（否则未渲染区域按旧行高撑高，滚动区露白）"""
    p, errors = probe_desktop
    p.fill(800)
    p.click_auto_scroll()  # 关跟随，避免贴底掩盖问题
    p.wait(300)
    p.scroll_to(5000)
    p.wait(300)
    before = p.state()

    p.page.set_viewport_size(MOBILE_VIEWPORT)
    p.wait(DETENT_SETTLE_MS)
    assert p.form() == "sheet", "窄屏未切到抽屉形态"

    after = p.state()
    assert after["scrollHeight"] != before["scrollHeight"], "总高未变（未按新宽度重测行高）"
    max_pos = int(after["scrollHeight"] - after["clientHeight"])
    assert max_pos > 0, "内容未铺满滚动区（无法验证滚动）"
    for pos in range(0, max_pos, 800):
        p.scroll_to(pos)
        p.wait(150)
        assert_no_gap(p.state(), f"切形态后滚动到 {pos}px")
    assert not errors


def test_short_viewport_shrinks_sheet(probe_mobile):
    """视口变矮（软键盘/地址栏）后抽屉高度跟随，恢复后回到原高度"""
    p, errors = probe_mobile
    p.fill(200)

    p.page.set_viewport_size({"width": 390, "height": 500})
    p.wait(DETENT_SETTLE_MS)
    assert abs(p.root_box()["h"] - round(500 * 0.55)) <= 1, f"变矮后高度未跟随：{p.root_box()['h']}"
    assert abs(p.root_box()["bottom"] - 500) <= 1, f"变矮后未贴底：{p.root_box()['bottom']}"
    assert_no_gap(p.state(), "视口变矮后")

    p.page.set_viewport_size(MOBILE_VIEWPORT)
    p.wait(DETENT_SETTLE_MS)
    assert abs(p.root_box()["h"] - MOBILE_HALF_HEIGHT) <= 1, "恢复视口后高度未回到半屏档"
    assert not errors


def test_side_panel_toggle_and_horizontal_layout(probe_mobile):
    """模块筛选条默认收起，展开后是横排可滚动条，筛选生效"""
    p, errors = probe_mobile
    p.fill(400)
    total = p.state()["shown"]
    assert p.side_open() is False, "筛选条初始应收起"
    assert p.run("() => document.querySelector('.lp-side') === null") is True, "收起时不应渲染侧栏"

    p.toggle_side()
    p.wait(300)
    assert p.side_open() is True, "开关未展开筛选条"
    rows = p.run(
        """() => Array.from(document.querySelectorAll('.lp-side .lp-tag-row')).slice(0, 2)
                 .map((el) => el.getBoundingClientRect())"""
    )
    assert len(rows) == 2, "筛选条内应有两个以上模块"
    assert abs(rows[0]["y"] - rows[1]["y"]) <= 1, "筛选条内模块未横向排列"
    assert rows[1]["x"] > rows[0]["x"], "筛选条内模块顺序异常"

    p.toggle_tag("scheduler")
    p.wait(300)
    assert p.state()["shown"] < total, "筛选条内点模块未生效"
    assert not errors


def test_sheet_session_does_not_touch_floating_geometry(probe_desktop):
    """抽屉会话不得改动悬浮几何：否则回到桌面窗口会变成手机尺寸"""
    p, errors = probe_desktop
    p.fill(50)

    p.page.set_viewport_size(MOBILE_VIEWPORT)
    p.wait(400)
    assert p.form() == "sheet", "窄屏未切到抽屉形态"
    during = p.geom_persisted()

    p.click_detent()
    p.wait(DETENT_SETTLE_MS)
    p.tap_title()
    p.wait(DETENT_SETTLE_MS)
    assert p.detent() == "half", "抽屉档位未来回切换（前置条件不成立）"
    assert p.geom_persisted() == during, "抽屉档位变化写进了悬浮几何"

    p.page.set_viewport_size(DESKTOP_VIEWPORT)
    p.wait(400)
    assert p.form() == "floating", "回到宽视口未切回悬浮形态"
    assert p.geom_persisted() == during, "形态往返后悬浮几何被改写"
    assert not errors
