"""LogPanel 探针页共享设施：构建/托管探针页 + 页面驱动器。

探针页（frontend/probe/）以组件源码 + 固定种子合成数据驱动组件，驱动器只经 DOM 读状态、
经真实交互（pointer 事件序列、按钮点击、输入事件）改状态——断言只依赖可见行为，
不触碰组件内部字段，组件实现变化不会让用例失效。

为什么用探针而不是真实 serve：虚拟列表与形态的缺陷只在「行数远多于视口、行高可变、
视口尺寸变化」时暴露，后端真实日志在测试里灌不到几千条。
"""

from __future__ import annotations

import functools
import http.server
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
PROBE_DIR = PROJECT_ROOT / "build-artifacts" / "logpanel-probe"
PROBE_URL = "/logpanel.html"

# 行高实测与布局收敛需要几帧，灌完数据后留出时间
SETTLE_MS = 2500
# 视口空白判定容差（px）：贴底时浏览器把 scrollTop 夹到合法值，末行底边与视口底边
# 存在亚像素级残差，并非真的没铺满
GAP_TOL = 1.0
# 抽屉高度过渡（CSS transition）的等待时间
DETENT_SETTLE_MS = 500


def build_and_serve_probe() -> Iterator[int]:
    """构建探针页并静态托管 build-artifacts/logpanel-probe，yield 端口"""
    subprocess.run("npm run build:probe", cwd=str(FRONTEND_DIR), check=True, shell=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PROBE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()


def open_probe(browser: Any, port: int, **context_kwargs: Any) -> tuple[Any, "Probe", list[str]]:
    """新建上下文打开探针页并等就绪；context_kwargs 透传 new_context（移动端模拟在此传入）"""
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://127.0.0.1:{port}{PROBE_URL}")
    page.wait_for_function("window.__ready === true", timeout=15000)
    return context, Probe(page), errors


def assert_no_gap(state: dict, context: str) -> None:
    """视口内不得出现顶部/底部空白"""
    top = state["topGap"] or 0
    bottom = state["bottomGap"] or 0
    assert top <= GAP_TOL and bottom <= GAP_TOL, f"{context}：顶部空白 {top}px，底部空白 {bottom}px"


class Probe:
    """探针驱动：evaluate / 手势 / 等待收在一处，避免测试里散落字符串"""

    def __init__(self, page: Any) -> None:
        self.page = page

    def run(self, js: str, arg: Any = None) -> Any:
        return self.page.evaluate(js, arg)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # ---- 数据与滚动 ----

    def push(self, n: int) -> int:
        return self.run("(n) => window.__probe.push(n)", n)

    def fill(self, n: int) -> None:
        """灌入 n 条并等布局收敛"""
        self.push(n)
        self.wait(SETTLE_MS)

    def state(self) -> dict:
        return self.run("() => window.__probe.state()")

    def wait_at_bottom(self, timeout_ms: int = 4000) -> None:
        """等贴底收敛：贴底由 rAF 驱动的测高/滚动管线完成，机器繁忙时需要多帧"""
        self.page.wait_for_function(
            f"() => window.__probe.state().distanceFromEnd <= {GAP_TOL}", timeout=timeout_ms
        )

    def scroll_to(self, offset: int) -> None:
        self.run("(v) => window.__probe.scrollTo(v)", offset)

    def line_text_at(self, index: int) -> str | None:
        return self.run("(i) => window.__probe.lineTextAt(i)", index)

    def line_top(self, index: int) -> float | None:
        return self.run("(i) => window.__probe.lineTop(i)", index)

    def first_expandable(self) -> int:
        return self.run("() => window.__probe.firstExpandable()")

    def expanded_texts(self) -> list[str]:
        return self.run("() => window.__probe.expandedTexts()")

    # ---- 面板控件 ----

    def hide(self) -> None:
        self.run("() => window.__probe.hide()")

    def show(self) -> None:
        self.run("() => window.__probe.show()")

    def click_pause(self) -> None:
        self.run("() => window.__probe.clickPause()")

    def click_auto_scroll(self) -> None:
        self.run("() => window.__probe.clickAutoScroll()")

    def click_clear(self) -> None:
        self.run("() => window.__probe.clickClear()")

    def click_line(self, index: int) -> None:
        self.run("(i) => window.__probe.clickLine(i)", index)

    def set_query(self, value: str) -> None:
        self.run("(v) => window.__probe.setQuery(v)", value)

    def toggle_regex(self) -> None:
        self.run("() => window.__probe.toggleRegex()")

    def toggle_level(self, level: int) -> None:
        self.run("(v) => window.__probe.toggleLevel(v)", level)

    def toggle_tag(self, tag: str) -> None:
        self.run("(v) => window.__probe.toggleTag(v)", tag)

    def open_menu(self) -> bool:
        return self.run("() => window.__probe.openMenu()")

    def menu_visible(self) -> bool:
        return self.run("() => window.__probe.menuVisible()")

    def click_menu_close(self) -> None:
        self.run("() => window.__probe.clickMenuClose()")

    # ---- 形态与抽屉 ----

    def set_form(self, mode: str) -> None:
        """锁定形态（auto/floating/sheet）：确定性测 DOM，不依赖媒体查询"""
        self.run("(m) => window.__probe.setForm(m)", mode)

    def form(self) -> str | None:
        return self.run("() => window.__probe.formOf()")

    def detent(self) -> str | None:
        return self.run("() => window.__probe.detentOf()")

    def root_box(self) -> dict:
        return self.run("() => window.__probe.rootBox()")

    def list_client_height(self) -> int:
        return self.run("() => window.__probe.listClientHeight()")

    def side_open(self) -> bool:
        return self.run("() => window.__probe.sideOpen()")

    def toggle_side(self) -> None:
        self.run("() => window.__probe.toggleSide()")

    def click_detent(self) -> None:
        self.run("() => window.__probe.clickDetent()")

    def tap_title(self) -> None:
        """标题栏点按（抽屉切档）"""
        self.run("() => window.__probe.tapTitle()")

    def drag_title(self, dy: int, duration_ms: int = 400) -> None:
        """标题栏拖拽：duration_ms 决定速度（快滑按速度翻档，慢拖按位移定档）"""
        self.run("([dy, ms]) => window.__probe.dragTitle(dy, ms)", [dy, duration_ms])

    def long_press_title(self) -> None:
        """长按标题栏（触屏打开菜单的路径）"""
        self.run("() => window.__probe.longPressTitle()")

    def inline_style_keys(self) -> list[str]:
        return self.run("() => window.__probe.inlineStyleKeys()")

    def geom_persisted(self) -> Any:
        return self.run("() => window.__probe.geomPersisted()")
