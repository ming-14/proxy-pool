"""LogPanel 虚拟视图 e2e（真实浏览器 + 可控日志源）。

为什么要组件级探针而不是走真实 serve：虚拟滚动的缺陷只在「行数远多于视口、
行高可变」时暴露，而后端真实日志无法在测试里灌到几千条的规模。
探针页 tests/e2e/web/logpanel_probe.html 直接 import 组件源码，随机数走固定种子，
断言因此可重复。

覆盖的不变式：
- 全程滚动不出现空白（顶部/底部露白）
- 环形缓冲淘汰时，锚定行保持在视口原位
- 估算行高收敛到实测均值
- 跟随模式下补窗不会因基准错位而空转
- 暂停 → 继续后，视图补上暂停期间到达的日志
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROBE_URL_PATH = "/tests/e2e/web/logpanel_probe.html"

# 测高是逐帧分批推进的，探针里灌完数据后需要给它收敛的时间
SETTLE_MS = 2500

# 视口空白判定容差（px）：贴底时浏览器会把 scrollTop 夹到合法值，末行底边与视口底边
# 存在亚像素级残差（实测 0.3px），并非真的没铺满
GAP_TOL = 1.0


@pytest.fixture(scope="module")
def server():
    """静态服务仓库根目录：探针页按相对路径 import 组件源码"""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PROJECT_ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture()
def probe(server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server}{PROBE_URL_PATH}")
        page.wait_for_function("window.__ready === true", timeout=10000)

        class Driver:
            """探针页驱动：把常用的 evaluate/wait 收在一处，避免测试里散落字符串"""

            def push(self, n):
                page.evaluate("(n) => window.__probe.push(n)", n)

            def state(self):
                return page.evaluate("() => window.__probe.state()")

            def wait(self, ms):
                page.wait_for_timeout(ms)

            def run(self, js, arg=None):
                return page.evaluate(js, arg)

        try:
            yield Driver(), errors
        finally:
            context.close()
            browser.close()


def _fill(probe, n: int):
    probe.push(n)
    probe.wait(SETTLE_MS)


def test_scrolling_full_range_has_no_gap(probe):
    """全程滚动：视口内不得出现顶部/底部空白"""
    p, errors = probe
    _fill(p, 1500)
    p.run("() => window.__probe.clickAutoScroll()")   # 关跟随，避免被强制贴底
    p.wait(300)
    s = p.state()
    maxpos = int(s["total"] - s["clientHeight"])
    gaps = []
    for pos in range(0, maxpos, 500):
        p.run("(v) => window.__probe.scrollTo(v)", pos)
        p.wait(120)
        cur = p.state()
        if (cur["topGap"] or 0) > GAP_TOL or (cur["bottomGap"] or 0) > GAP_TOL:
            gaps.append((pos, cur["topGap"], cur["bottomGap"]))
    assert not gaps, f"滚动出现空白：{gaps[:5]}"
    assert not errors


def test_trim_compensation_keeps_anchor_line(probe):
    """缓冲满后继续推送：未被淘汰的锚定行必须留在视口原位"""
    p, errors = probe
    _fill(p, 1500)
    _fill(p, 600)          # 灌到容量上限 2000，之后每条新日志都会淘汰一条旧的
    p.run("() => window.__probe.scrollTo(20000)")
    p.wait(500)
    before_text = p.run("() => window.__probe.lineTextAt(10)")
    before_top = p.run("() => window.__probe.lineTop(10)")
    p.push(100)
    p.wait(800)
    after_text = p.run("() => window.__probe.lineTextAt(10)")
    after_top = p.run("() => window.__probe.lineTop(10)")
    assert before_text == after_text, "锚定行被换掉了"
    assert abs(after_top - before_top) <= 1, f"锚定行漂移 {after_top - before_top}px"
    assert not errors


def test_estimated_height_converges_to_measured_mean(probe):
    """估算行高必须收敛到实测均值（否则窗口行数算偏，靠补窗兜底）"""
    p, errors = probe
    _fill(p, 1200)
    for width in (420, 900):
        p.run("(w) => window.__probe.resize(w, 480)", width)
        p.wait(SETTLE_MS)
        s = p.state()
        assert s["avgRow"] > 0
        dev = abs(s["estHeight"] - s["avgRow"]) / s["avgRow"]
        assert dev < 0.1, f"宽度 {width}: estHeight={s['estHeight']} 实测均值={s['avgRow']:.1f} 偏差 {dev:.0%}"
        assert (s["topGap"] or 0) <= GAP_TOL and (s["bottomGap"] or 0) <= GAP_TOL, "resize 后出现空白"
    assert not errors


def test_follow_mode_does_not_reexpand_window(probe):
    """跟随模式下补窗不得因基准错位而空转（每帧重复重建窗口）"""
    p, errors = probe
    _fill(p, 500)
    p.run("() => window.__probe.resetCounters()")
    for _ in range(12):
        p.push(50)
        p.wait(120)
    p.wait(600)
    counters = p.run("() => window.__probe.counters()")
    s = p.state()
    assert counters["coverage"] == 0, f"跟随模式补窗改动窗口 {counters['coverage']} 次（应为 0）"
    # 每轮推送最多 MAX_LAYOUT_PASSES 次修正渲染，留一倍余量；误扩时实测可达 8+ 次/轮
    per_round = counters["paint"] / 12
    assert per_round < 6, f"每轮推送重建窗口 {per_round:.1f} 次，疑似补窗空转"
    assert s["scrollTop"] + s["clientHeight"] == s["scrollHeight"], "跟随模式未贴底"
    assert not errors


def test_resume_after_pause_catches_up(probe):
    """暂停期间到达的日志，继续后必须补进视图（不能停在暂停那一刻）"""
    p, errors = probe
    _fill(p, 300)
    p.run("() => window.__probe.clickPause()")
    p.wait(150)
    assert p.state()["paused"] is True
    before = p.state()["viewLen"]
    p.push(300)
    p.wait(600)
    during = p.state()["viewLen"]
    assert during == before, "暂停期间视图不应变化"
    p.run("() => window.__probe.clickPause()")
    p.wait(800)
    after = p.run("() => window.__probe.state()")
    assert after["viewLen"] == before + 300, (
        f"继续后视图未补上暂停期间的日志：{before} -> {after['viewLen']}，期望 {before + 300}"
    )
    assert not errors
