"""Web 面板浏览器 e2e：真实 serve 子进程（8082/8083）+ mock 上游代理 + Playwright Chromium

覆盖：壳层导航/主题/暂停轮询、仪表盘 KPI 与三趋势图与地图、代理组（过滤/排序/添加/
右键拉黑还原/框选/导出）、队列（表格/固定/锁定/hover 浮窗）、设置页（调度参数保存）、
代理源（暂停恢复）。截图输出到 build-artifacts/e2e-screenshots/ 供与参考图人工比对。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from tests.e2e.web.conftest import BASE_URL, api_status

SHOT_DIR = Path(__file__).resolve().parents[3] / "build-artifacts" / "e2e-screenshots"


@pytest.fixture()
def shot(page):
    """显式截图辅助：shot('name') 输出到 build-artifacts/e2e-screenshots/"""

    def _shot(name: str):
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOT_DIR / f"{name}.png"))

    return _shot


# ---------------------------------------------------------------- 壳层
class TestShell:
    def test_dashboard_loaded(self, page):
        assert page.locator('[data-testid="kpi-grid"] .kpi-value').count() == 4
        assert page.locator('[data-testid="conn-text"]').inner_text() == "已连接"
        assert page.locator('[data-testid="page-title"]').inner_text() == "仪表盘"

    def test_nav_routing(self, page):
        def goto_expect(nav: str, title: str, frag: str):
            page.click(f'[data-testid="nav-{nav}"]')
            page.wait_for_function(
                "t => document.querySelector('[data-testid=page-title]').textContent === t",
                arg=title, timeout=5000,
            )
            assert frag in page.url

        goto_expect("groups", "代理组", "#/groups")
        goto_expect("standby", "代理服务队列", "#/standby")
        goto_expect("sources", "代理源", "#/sources")
        goto_expect("settings", "设置", "#/settings")
        goto_expect("dashboard", "仪表盘", "#/dashboard")

    def test_theme_toggle_persists(self, page):
        page.click('[data-testid="btn-theme"]')
        assert page.evaluate("document.documentElement.classList.contains('dark')")
        assert page.evaluate("localStorage.getItem('proxy-pool-theme')") == "dark"
        page.reload()
        page.wait_for_selector('[data-testid="kpi-grid"]')
        assert page.evaluate("document.documentElement.classList.contains('dark')")
        page.click('[data-testid="btn-theme"]')
        assert not page.evaluate("document.documentElement.classList.contains('dark')")

    def test_pause_resume_polling(self, page):
        page.click('[data-testid="btn-pause"]')
        assert page.locator('[data-testid="btn-pause"]').inner_text().strip() == "继续"
        snap1 = page.evaluate("document.querySelector('[data-testid=kpi-available] .kpi-value').textContent")
        # 暂停期间制造数据变化（API 加代理），KPI 不应刷新
        requests.post(f"{BASE_URL}/api/group/default-http/add", json={"proxies": ["http://9.9.9.9:1"]}, timeout=5)
        page.wait_for_timeout(3000)
        snap2 = page.evaluate("document.querySelector('[data-testid=kpi-available] .kpi-value').textContent")
        assert snap1 == snap2
        page.click('[data-testid="btn-pause"]')
        assert page.locator('[data-testid="btn-pause"]').inner_text().strip() == "暂停"
        page.wait_for_selector('[data-testid="kpi-available"]')
        deadline = time.time() + 8
        while time.time() < deadline:
            if page.evaluate("document.querySelector('[data-testid=kpi-available] .kpi-value').textContent") != snap2:
                break
            page.wait_for_timeout(500)
        # 清理：删除测试代理
        requests.post(f"{BASE_URL}/api/proxy/remove", json={"proxies": ["http://9.9.9.9:1"], "group": "default-http"}, timeout=5)


# ---------------------------------------------------------------- 仪表盘
class TestDashboard:
    def test_trend_charts_render(self, page):
        for tid in ("latency-chart", "health-chart", "traffic-chart"):
            canvas = page.locator(f'[data-testid="{tid}"] canvas')
            canvas.wait_for(state="visible", timeout=10000)
            assert canvas.bounding_box()["height"] > 100

    def test_traffic_kpi(self, page):
        text = page.locator('[data-testid="traffic-kpi"]').inner_text()
        for label in ("实时上行", "实时下行", "累计流量", "平均延迟"):
            assert label in text

    def test_geo_map(self, page):
        page.locator('[data-testid="geo-map"] canvas').wait_for(state="visible", timeout=20000)
        stats = page.locator('[data-testid="geo-stats"]').inner_text()
        assert "总 IP" in stats and "覆盖国家/地区" in stats

    def test_kpi_matches_api(self, page):
        api = api_status()
        expect = str(api["available_count"])
        page.wait_for_function(
            "v => document.querySelector('[data-testid=kpi-available] .kpi-value').textContent === v",
            arg=expect, timeout=10000,
        )

    def test_dashboard_dark_screenshot(self, page, shot):
        page.click('[data-testid="btn-theme"]')  # 切暗色，与参考图一致
        page.wait_for_timeout(6500)  # 等历史积累若干点 + 地图数据
        shot("dashboard-dark")
        page.click('[data-testid="btn-theme"]')


# ---------------------------------------------------------------- 代理组
class TestGroups:
    @pytest.fixture(autouse=True)
    def _goto(self, page):
        page.click('[data-testid="nav-groups"]')
        page.wait_for_selector('[data-testid="grp-select"]')
        # staging 为非可请求组：不会被 standby fill 搬走，数据稳定
        page.select_option('[data-testid="grp-select"]', "staging")
        page.wait_for_selector('[data-testid="grp-table"] tbody tr[data-proxy]')

    def test_rows_and_info(self, page):
        rows = page.locator('[data-testid="grp-table"] tbody tr[data-proxy]')
        assert rows.count() == 3
        assert "共 3 条" in page.locator('[data-testid="grp-info"]').inner_text()

    def test_filter(self, page):
        page.fill('[data-testid="grp-filter"]', "127.0.0.1")
        page.wait_for_timeout(300)
        assert page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').count() == 3
        page.fill('[data-testid="grp-filter"]', "no-such-host-xyz")
        page.wait_for_timeout(300)
        assert page.locator("text=无匹配代理").count() == 1
        page.fill('[data-testid="grp-filter"]', "")

    def test_sort_by_latency(self, page):
        page.click("th.sortable:has-text('延迟')")
        page.wait_for_timeout(300)
        page.click("th.sortable:has-text('延迟')")
        page.wait_for_timeout(300)

    def test_add_and_delete(self, page):
        before = page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').count()
        page.click('[data-testid="grp-add-btn"]')
        page.wait_for_selector(".modal-overlay")
        page.fill(".modal textarea", "203.0.113.5:8080")
        page.click(".modal-footer .btn-primary")
        page.wait_for_selector(".toast-success", timeout=8000)
        page.wait_for_timeout(500)
        assert page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').count() == before + 1
        # 清理：右键删除
        row = page.locator('tr[data-proxy="http://203.0.113.5:8080"]')
        row.click(button="right")
        page.wait_for_selector(".context-menu")
        page.click(".context-menu-item:has-text('删除')")
        page.wait_for_selector(".toast-success", timeout=8000)
        page.wait_for_timeout(500)
        assert page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').count() == before

    def test_blacklist_restore_flow(self, page):
        row = page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').first
        proxy = row.get_attribute("data-proxy")
        row.click(button="right")
        page.wait_for_selector(".context-menu")
        page.click(".context-menu-item:has-text('拉黑')")
        page.wait_for_selector(".toast-success", timeout=8000)
        # 黑名单组出现该代理
        page.select_option('[data-testid="grp-select"]', "blacklist")
        page.wait_for_timeout(800)
        assert page.locator(f'[data-testid="grp-table"] tr[data-proxy="{proxy}"]').count() == 1
        # 右键还原（黑名单组菜单为 还原/删除）
        page.locator(f'[data-testid="grp-table"] tr[data-proxy="{proxy}"]').click(button="right")
        page.wait_for_selector(".context-menu")
        page.click(".context-menu-item:has-text('还原')")
        page.wait_for_selector(".toast-success", timeout=8000)
        page.select_option('[data-testid="grp-select"]', "staging")
        page.wait_for_timeout(800)
        assert page.locator(f'[data-testid="grp-table"] tr[data-proxy="{proxy}"]').count() == 1

    def test_move_submenu(self, page):
        row = page.locator('[data-testid="grp-table"] tbody tr[data-proxy]').first
        proxy = row.get_attribute("data-proxy")
        row.click(button="right")
        page.wait_for_selector(".context-menu")
        page.hover(".context-menu-item:has-text('移动')")
        page.wait_for_selector(".context-submenu .context-menu-item")
        page.click(".context-submenu .context-menu-item:has-text('信任池')")
        page.wait_for_selector(".toast-success", timeout=8000)
        # 移回 staging（若已被 fill 搬入队列，move 仍按代理当前组移动）
        requests.post(f"{BASE_URL}/api/proxy/move", json={"proxies": [proxy], "target_group": "staging"}, timeout=5)

    def test_marquee_selection(self, page, shot):
        wrap = page.locator('[data-testid="grp-table"]')
        box = wrap.bounding_box()
        y0 = box["y"] + 80
        page.mouse.move(box["x"] + 30, y0)
        page.mouse.down()
        page.mouse.move(box["x"] + 30, y0 + 90, steps=8)
        shot("marquee")  # 框选中截图（松开前）
        page.mouse.up()
        selected = page.locator('[data-testid="grp-table"] tbody tr.selected').count()
        assert selected >= 2
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert page.locator('[data-testid="grp-table"] tbody tr.selected').count() == 0
        page.keyboard.press("Control+a")
        page.wait_for_timeout(200)
        assert page.locator('[data-testid="grp-table"] tbody tr.selected').count() >= 3

    def test_export_csv_download(self, page):
        with page.expect_download(timeout=10000) as dl:
            page.click('[data-testid="grp-export-csv"]')
        download = dl.value
        assert download.suggested_filename == "staging.csv"
        download.cancel()

    def test_groups_screenshot(self, page, shot):
        page.click('[data-testid="btn-theme"]')
        shot("groups-dark")
        page.click('[data-testid="btn-theme"]')


# ---------------------------------------------------------------- 队列页
class TestStandby:
    @pytest.fixture(autouse=True)
    def _goto(self, page):
        page.click('[data-testid="nav-standby"]')
        page.wait_for_selector('[data-testid="sb-table"] tbody tr[data-proxy]')

    def test_queue_rows_match_api(self, page):
        n = page.locator('[data-testid="sb-table"] tbody tr[data-proxy]').count()
        assert n == api_status()["standby_count"] >= 1

    def test_hover_tooltip_with_charts(self, page, shot):
        code = page.locator('[data-testid="sb-table"] tbody tr code').first
        code.hover()
        tip = page.locator(".proxy-tooltip")
        tip.wait_for(state="visible", timeout=5000)
        assert tip.locator("canvas").count() == 2
        assert "测速" in tip.inner_text()
        # 定位必须在视口内（曾因 left/top 拼出非法值 "258px}" 被浏览器忽略，
        # 浮窗落回静态位置；mock 行数少恰好可见，真实数据下移出屏幕）
        box = tip.bounding_box()
        vp = page.evaluate("[innerWidth, innerHeight]")
        assert 0 <= box["x"] and box["x"] + box["width"] <= vp[0]
        assert 0 <= box["y"] and box["y"] + box["height"] <= vp[1]
        shot("tooltip-dark")
        page.mouse.move(5, 5)
        tip.wait_for(state="hidden", timeout=5000)

    def test_pin_unpin(self, page):
        row = page.locator('[data-testid="sb-table"] tbody tr[data-proxy]').first
        proxy = row.get_attribute("data-proxy")
        row.locator('button[data-action="pin"]').click()
        page.wait_for_selector(".toast-success", timeout=8000)
        page.wait_for_timeout(2500)
        row2 = page.locator(f'[data-testid="sb-table"] tr[data-proxy="{proxy}"]')
        assert row2.locator('button[data-action="unpin"]').count() == 1
        row2.locator('button[data-action="unpin"]').click()
        page.wait_for_timeout(2500)

    def test_lock_unlock(self, page):
        row = page.locator('[data-testid="sb-table"] tbody tr[data-proxy]').first
        row.locator('button[data-action="lock"]').click()
        page.wait_for_selector('[data-testid="lock-card"]', timeout=8000)
        # 锁定后仪表盘隐藏健康率卡
        page.click('[data-testid="nav-dashboard"]')
        page.wait_for_timeout(500)
        assert page.locator('[data-testid="health-card"]').count() == 0
        assert page.locator('[data-testid="kpi-standby"] .kpi-value').inner_text() == "固定代理"
        page.click('[data-testid="nav-standby"]')
        page.click('[data-testid="unlock-btn"]')
        page.wait_for_selector('[data-testid="lock-card"]', state="hidden", timeout=8000)

    def test_settings_save(self, page):
        # 调度参数已迁至设置页（队列页不再展示）
        page.click('[data-testid="nav-settings"]')
        page.wait_for_selector('[data-testid="settings-card"]', timeout=8000)
        page.fill('[data-testid="set-interval"]', "5")
        save = page.locator('[data-testid="set-save"]')
        save.wait_for(state="visible")
        assert save.is_enabled()
        save.click()
        page.wait_for_selector(".toast-success", timeout=8000)
        assert api_status()["settings"]["interval"] == 5
        # 还原
        requests.post(f"{BASE_URL}/api/settings", json={"interval": 2}, timeout=5)
        page.wait_for_timeout(2500)
        assert page.input_value('[data-testid="set-interval"]') == "2"

    def test_standby_dark_screenshot(self, page, shot):
        page.click('[data-testid="btn-theme"]')
        shot("standby-dark")
        page.click('[data-testid="btn-theme"]')


# ---------------------------------------------------------------- 代理源
class TestSources:
    @pytest.fixture(autouse=True)
    def _goto(self, page):
        page.click('[data-testid="nav-sources"]')
        page.wait_for_selector('[data-testid="src-table"] tbody tr')

    def test_list_and_pause_resume(self, page):
        rows = page.locator('[data-testid="src-table"] tbody tr')
        assert rows.count() == len(api_status()["sources"])
        btn = page.locator('button[data-action="pause"]').first
        btn.click()
        page.wait_for_selector(".toast-success", timeout=8000)
        page.wait_for_timeout(2500)
        resume = page.locator('button[data-action="resume"]')
        assert resume.count() >= 1
        resume.first.click()
        page.wait_for_selector(".toast-success", timeout=8000)


# ---------------------------------------------------------------- 控制台日志面板
class TestLogPanel:
    """侧边栏底部按钮启停的控制台日志面板（shadow DOM 渲染）"""

    def test_open_shows_entries(self, page):
        """打开面板：按钮激活、面板可见、日志行渲染、标题正确"""
        btn = page.locator('[data-testid="btn-logpanel"]')
        assert btn.is_visible()
        btn.click()
        # 面板 host 可见（shadow DOM 内渲染 .lp-log-line）
        page.wait_for_function("""() => {
            const host = document.querySelector('#logpanel-host');
            if (!host || host.hasAttribute('data-hidden') || !host.shadowRoot) return false;
            return host.shadowRoot.querySelector('.lp-log-line') !== null;
        }""", timeout=10000)
        # 标题与模块标签（控制台日志 + 无 src. 前缀）
        title = page.evaluate("document.querySelector('#logpanel-host').shadowRoot.querySelector('.lp-title span').textContent")
        assert title == "控制台日志"
        tag_text = page.evaluate("document.querySelector('#logpanel-host').shadowRoot.querySelector('.lp-log-line .lp-tag').textContent")
        assert tag_text and "src." not in tag_text
        # 按钮呈激活态（打开时主题色高亮）
        assert 'btn-primary' in (btn.get_attribute('class') or '')
        # 关闭后按钮恢复 ghost 态
        btn.click()
        assert page.evaluate("document.querySelector('#logpanel-host').hasAttribute('data-hidden')")
        assert 'btn-primary' not in (btn.get_attribute('class') or '')

    def test_hidden_stops_polling(self, page):
        """性能约束：面板隐藏时零 /api/logs 请求，重新打开恢复"""
        logs_seen: list[float] = []
        page.on("request", lambda r: logs_seen.append(time.time()) if "/api/logs" in r.url else None)
        # 打开 → 应出现请求
        page.click('[data-testid="btn-logpanel"]')
        deadline = time.time() + 10
        while time.time() < deadline and not logs_seen:
            page.wait_for_timeout(200)
        assert logs_seen, "面板打开后应产生 /api/logs 请求"
        # 关闭 → 3.2s（> 1s 轮询间隔）内零新请求
        page.click('[data-testid="btn-logpanel"]')
        count_at_close = len(logs_seen)
        page.wait_for_timeout(3200)
        assert len(logs_seen) == count_at_close, "面板隐藏后不应再请求 /api/logs"
        # 重新打开 → 恢复请求
        page.click('[data-testid="btn-logpanel"]')
        deadline = time.time() + 10
        while time.time() < deadline and len(logs_seen) == count_at_close:
            page.wait_for_timeout(200)
        assert len(logs_seen) > count_at_close, "重新打开后应恢复 /api/logs 请求"
