"""Web 控制面板 API 测试：注入 fake container/standby/server，覆盖读与写各端点

保持工程测试惯例：真实 ProxyPool 作为数据后端，容器组件（fetch/standby/
server）用 mock，隔离网络与 pproxy。用 aiohttp TestClient 直接驱动路由。
"""

import asyncio
import logging
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from aiohttp.test_utils import TestClient, TestServer

from src.domain.proxy_pool import ProxyPool, LOCKED_GROUP
from src.domain.models import SourceSpec
from src.use_cases.fetch_proxies import FetchProxiesUseCase
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework.config import (
    PROXY_GROUPS,
    SOURCES,
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    SOURCE_DEDUP_THRESHOLD,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
    STANDBY_GROUP,
    STANDBY_FILL_PROBE_MULTIPLIER,
    STANDBY_FILL_PROBE_MIN_EXTRA,
    METRICS_WINDOW,
)
from src.framework.web.control import ServeControl
from src.framework.web.api import build_app, PanelContext, _do_fetch
from src.framework.web.panel_state import PanelTasks
from src.framework import logbuffer
from src.use_cases.standby import ProxyLatencySample
from src.interfaces.proxy_server import TrafficSample


def make_pool():
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "default-https", "default-socks5",
                            "freeproxy-world-http", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


class FakeStandby:
    """代理服务队列壳：站内读取走真实 pool"""

    # 采样数据留存时长（全局，流量与队列指标共用）：/api/status/history 下发给前端裁剪缓冲
    metrics_window = METRICS_WINDOW
    # 队列页图表的显示窗口（/api/standby/series 下发给前端裁剪本地缓冲），取配置默认值
    chart_window = 60

    def __init__(self, pool):
        self.pool = pool
        self.probe_params = []

    @property
    def standby_group(self):
        return STANDBY_GROUP

    def standby_proxies(self, *, truncate=True):
        # 完整队列（不因锁定状态改变，用于展示/指标）
        # FakeStandby 不截断（测试用池无 capacity 概念），truncate 参数保持接口兼容
        return list(self.pool.proxy_groups.get(STANDBY_GROUP, {}).get("proxies", {}))

    def upstream_proxies(self):
        # 锁定状态：只返回锁定组代理（pproxy 唯一上游）；否则返回完整队列
        locked = list(self.pool.proxy_groups.get(LOCKED_GROUP, {}).get("proxies", {}))
        if locked:
            return locked
        return self.standby_proxies()

    def pending_retries(self):
        return []

    def is_locked(self):
        return bool(self.pool.proxy_groups.get(LOCKED_GROUP, {}).get("proxies", {}))

    def lock(self, proxy):
        self.pool.move_proxy(proxy, LOCKED_GROUP, consecutive_fails=0)

    def unlock(self):
        unlocked = None
        for p in list(self.pool.proxy_groups.get(LOCKED_GROUP, {}).get("proxies", {})):
            self.pool.move_proxy(p, STANDBY_GROUP, consecutive_fails=0)
            unlocked = p
        return unlocked

    def history(self):
        return []

    def per_proxy_latency_history(self, bind):
        return []

    def update_fill_probe_params(self, multiplier, min_extra):
        self.probe_params.append((multiplier, min_extra))


def make_purity_mock() -> MagicMock:
    """纯净度用例 mock：源元数据为字符串（快照会下发到 JSON），默认无缓存命中"""
    purity = MagicMock()
    purity.source = "abuseipdb"
    purity.label = "AbuseIPDB"
    purity.query_cache = MagicMock(return_value=None)  # 无缓存 → _purity_cached 返回 None
    return purity


class _FakeRequest:
    """只喂 _do_fetch 依赖的两样东西：取 PanelContext 的 app 与 JSON body

    直接驱动 handler 而不走 TestClient，才能稳定制造「请求协程被取消」那一幕
    （客户端断开不一定让服务端 handler 跟着取消）。
    """

    def __init__(self, app, body):
        self.app = app
        self._body = body

    async def json(self):
        return self._body


def make_ctx(pool):
    fetch = MagicMock()
    fetch.collect.return_value = []  # 无拉取结果：路由会遍历它判定是否全部源失败
    record = RecordProxyUseCase(pool=pool, standby_group=STANDBY_GROUP)
    standby = FakeStandby(pool)
    server = MagicMock()
    server.sync = MagicMock()
    server.set_salgorithm = AsyncMock()
    server.traffic_history = MagicMock(return_value=[])
    server.traffic_total = MagicMock(return_value=(0, 0))
    server.per_proxy_traffic_history = MagicMock(return_value=[])
    control = ServeControl(
        interval=60, concurrency=4, timeout=5, probe_url="https://probe.local/ip",
        allowed_types={"http", "socks5"},
        fill_probe_multiplier=STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=STANDBY_FILL_PROBE_MIN_EXTRA,
    )
    purity = make_purity_mock()
    geo = MagicMock()
    # IP 归属：默认无缓存、无定位结果（group detail 正常路径，避免解包异常被吞）
    geo.query_cache = MagicMock(return_value=({}, []))
    geo.locate = MagicMock(return_value={})
    geo.save_cache = MagicMock()
    ctx = PanelContext(pool=pool, fetch=fetch, record=record, standby=standby,
                       control=control, server=server, save=MagicMock(), capacity=5,
                       purity=purity, geo=geo, tasks=PanelTasks())
    return ctx, fetch, standby, server


class PanelTasksTest(unittest.TestCase):
    """面板任务状态注册表单测：幂等 / 结果存储 / FIFO 容量 / purity 按 IP 共享"""

    def test_fetch_gate_allows_single_run(self):
        """拉取全局单任务：进行中时第二次 start 返回 False（并发拉取直接拒绝）"""
        tasks = PanelTasks()
        assert tasks.fetch_start(None) is True  # 全量
        assert tasks.fetch_start("src.a") is False
        assert tasks.fetch_start(None) is False
        tasks.fetch_done()
        assert tasks.fetch_start("src.a") is True  # 结束后可再起（单源）

    def test_fetch_state_covers_target_source(self):
        """中间态归属：全量拉取覆盖所有源，单源拉取只覆盖目标源"""
        tasks = PanelTasks()
        tasks.fetch_start(None)
        assert tasks.fetching_all() is True
        assert tasks.is_fetching("src.a") is True and tasks.is_fetching("src.b") is True
        tasks.fetch_done()
        tasks.fetch_start("src.a")
        assert tasks.fetching_all() is False
        assert tasks.is_fetching("src.a") is True and tasks.is_fetching("src.b") is False

    def test_speedtest_start_idempotent(self):
        """同一 bind 已在 running 时第二次 start 返回 False（不重复起任务）"""
        tasks = PanelTasks()
        assert tasks.speedtest_start("1.2.3.4:8080") is True
        assert tasks.speedtest_start("1.2.3.4:8080") is False

    def test_speedtest_done_then_restart(self):
        """done 保存结果与时间戳；终态后允许重新 start（重测）"""
        tasks = PanelTasks()
        tasks.speedtest_start("1.2.3.4:8080")
        tasks.speedtest_done("1.2.3.4:8080", {"proxy": "http://1.2.3.4:8080"})
        st = tasks.speedtest_of("1.2.3.4:8080")
        assert st["status"] == "done"
        assert st["result"]["proxy"] == "http://1.2.3.4:8080"
        assert st["ts"] is not None
        assert tasks.speedtest_start("1.2.3.4:8080") is True

    def test_speedtest_error(self):
        """error 保存错误文案；of 读取终态"""
        tasks = PanelTasks()
        tasks.speedtest_start("1.2.3.4:8080")
        tasks.speedtest_error("1.2.3.4:8080", "boom")
        assert tasks.speedtest_of("1.2.3.4:8080")["status"] == "error"
        assert tasks.speedtest_of("1.2.3.4:8080")["error"] == "boom"

    def test_purity_keyed_by_ip_shared(self):
        """purity 按 IP 键：同 IP 不同端口行共享 checking（防重复调 API 日限额）"""
        tasks = PanelTasks()
        assert tasks.purity_start("1.2.3.4") is True
        assert tasks.purity_start("1.2.3.4") is False
        tasks.purity_done("1.2.3.4")
        assert tasks.purity_of("1.2.3.4") is None
        assert tasks.purity_start("1.2.3.4") is True  # 完成后可重新查

    def test_purity_error_cleared_by_done(self):
        """error 后重查：start 覆盖旧终态"""
        tasks = PanelTasks()
        tasks.purity_start("1.2.3.4")
        tasks.purity_error("1.2.3.4", "API down")
        assert tasks.purity_of("1.2.3.4")["status"] == "error"
        assert tasks.purity_start("1.2.3.4") is True

    def test_fifo_capacity_evicts_stale(self):
        """超容量优先淘汰最旧的非中间态条目"""
        tasks = PanelTasks(capacity=2)
        tasks.speedtest_start("a"); tasks.speedtest_done("a", {})
        tasks.speedtest_start("b"); tasks.speedtest_done("b", {})
        tasks.speedtest_start("c")
        assert tasks.speedtest_of("a") is None
        assert tasks.speedtest_of("b") is not None
        assert tasks.speedtest_of("c") is not None

    def test_fifo_capacity_all_running(self):
        """全 running 时退而淘汰最旧"""
        tasks = PanelTasks(capacity=2)
        tasks.speedtest_start("a")
        tasks.speedtest_start("b")
        tasks.speedtest_start("c")
        assert tasks.speedtest_of("a") is None
        assert tasks.speedtest_of("b") is not None
        assert tasks.speedtest_of("c") is not None


class FetchRouteIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """拉取路由 × 真实用例：网络段走 executor 线程、入池段回事件循环线程

    上面 WebApiTest 用 MagicMock 验编排契约，这里用真实 FetchProxiesUseCase（loader 注入
    假源，不打网络）验整条路径——三段拆分后结果仍要能入池、中间态仍要清干净。
    """

    async def asyncSetUp(self):
        self.pool = make_pool()
        self.ctx, _, _, _ = make_ctx(self.pool)
        self.spec = SourceSpec(**SOURCES[0])
        self.loaded: list[str] = []

        def loader(name):
            self.loaded.append(name)
            fake = MagicMock()
            fake.fetch_proxies.return_value = ["http://5.6.7.8:3128"]
            return fake

        self.ctx.fetch = FetchProxiesUseCase(
            pool=self.pool, sources=[self.spec], loader=loader,
            fetch_retries=1, dedup_threshold=SOURCE_DEDUP_THRESHOLD)
        self.client = TestClient(TestServer(build_app(self.ctx)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_fetch_merges_and_clears_state(self):
        resp = await self.client.post("/api/fetch", json={"source": self.spec.name})
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True and body["available"] == 1
        assert self.loaded == [self.spec.name]
        assert "http://5.6.7.8:3128" in self.pool.get_available_proxies()
        # 中间态已清（拉取结束后快照不再显示「处理中」）
        assert self.ctx.tasks.fetching_all() is False
        assert self.ctx.tasks.is_fetching(self.spec.name) is False

    async def test_fetch_all_sources_failed_reports_error(self):
        """全部源拉取异常 → 500（不谎报「拉取完成 0 个」），中间态照常清空"""
        def loader(_name):
            fake = MagicMock()
            fake.fetch_proxies.side_effect = RuntimeError("源挂了")
            return fake

        self.ctx.fetch = FetchProxiesUseCase(
            pool=self.pool, sources=[self.spec], loader=loader,
            fetch_retries=1, dedup_threshold=SOURCE_DEDUP_THRESHOLD)
        resp = await self.client.post("/api/fetch", json={"source": self.spec.name})
        assert resp.status == 500
        assert "全部代理源拉取失败" in (await resp.json())["error"]
        assert self.pool.get_available_proxies() == {}  # 拉取失败的源不入池
        assert self.ctx.tasks.is_fetching(self.spec.name) is False

    async def test_fetch_plan_failure_clears_state(self):
        """选源（plan）抛异常也必须清中间态：它在任务 try 内，否则面板永久「拉取中」"""
        plan = self.ctx.fetch.plan
        self.ctx.fetch.plan = MagicMock(side_effect=RuntimeError("选源挂了"))
        resp = await self.client.post("/api/fetch", json={})
        assert resp.status == 500
        assert self.ctx.tasks.fetching_all() is False
        # 闸门已释放：选源恢复后下一次拉取照常放行
        self.ctx.fetch.plan = plan
        resp = await self.client.post("/api/fetch", json={})
        assert resp.status == 200

    async def test_fetch_survives_request_cancellation(self):
        """客户端刷新/关页取消请求协程：后台拉取照常入池并自清中间态

        HTTP 层无法稳定制造服务端 handler 取消，故直接驱动 _do_fetch：假源在
        executor 里阻塞 0.2s，期间取消请求协程，验证 shield 让任务独立跑完。
        """
        def slow_source(retries=None):
            time.sleep(0.2)  # 让取消动作落在拉取进行中，而非已经跑完
            return ["http://5.6.7.8:3128"]

        def slow_loader(_name):
            fake = MagicMock()
            fake.fetch_proxies.side_effect = slow_source
            return fake

        self.ctx.fetch = FetchProxiesUseCase(
            pool=self.pool, sources=[self.spec], loader=slow_loader,
            fetch_retries=1, dedup_threshold=SOURCE_DEDUP_THRESHOLD)
        handler = asyncio.ensure_future(_do_fetch(_FakeRequest(self.client.server.app, {})))
        await asyncio.sleep(0.05)  # 已过闸门、拉取正挂在 executor 里
        assert self.ctx.tasks.fetching_all() is True
        handler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler
        for _ in range(100):  # 后台任务不随请求协程一起死
            if not self.ctx.tasks.fetching_all():
                break
            await asyncio.sleep(0.02)
        assert self.ctx.tasks.fetching_all() is False
        assert "http://5.6.7.8:3128" in self.pool.get_available_proxies()


class WebApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = make_pool()
        self.ctx, self.fetch, self.standby, self.server = make_ctx(self.pool)
        self.client = TestClient(TestServer(build_app(self.ctx)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_index_serves_html(self):
        resp = await self.client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "控制面板" in body and "<script>" in body

    async def test_status_snapshot(self):
        self.pool.add_to_group(STANDBY_GROUP, "1.2.3.4:8080", consecutive_fails=0, latency=0.5)
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby_count"] == 1
        assert data["geo_enabled"] is True
        assert data["standby"][0]["proxy"] == "http://1.2.3.4:8080"
        assert data["settings"]["salgorithm"] == "rc"
        assert data["settings"]["allowed_types"] == ["http", "socks5"]
        assert any(s["name"] for s in data["sources"])
        # 拉取中间态随快照下发（前端刷新后据此恢复按钮的「处理中」）
        assert data["fetching_all"] is False
        assert all(s["fetching"] is False for s in data["sources"])
        assert "Proxy-pool-standby-queue" in data["groups"]
        # 历史数据已从 status 快照分离（体量大，改走 /api/status/history 增量拉取）
        assert "standby_history" not in data
        assert "traffic_history" not in data
        assert data["traffic_total"] == {"upload": 0, "download": 0}

    async def test_status_history(self):
        """历史端点：缺省返回全量（空数据时为空数组），since 过滤返回空"""
        resp = await self.client.get("/api/status/history")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby_history"] == []
        assert data["traffic_history"] == []
        assert data["traffic_total"] == {"upload": 0, "download": 0}
        # 留存时长随响应下发（前端据此按时间裁剪本地缓冲，不写死点数上限）
        assert data["retention_window"] == METRICS_WINDOW
        # since 过滤：无晚于游标的数据返回空
        resp2 = await self.client.get("/api/status/history?since=9999999999")
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["standby_history"] == []
        assert data2["traffic_history"] == []

    async def test_status_history_invalid_since(self):
        """since 非数字 → 400（不落成 500）"""
        resp = await self.client.get("/api/status/history?since=abc")
        assert resp.status == 400

    async def test_standby_series_full_and_incremental(self):
        """逐代理序列：首次全量（含失败点 latency=None），游标增量只返回新点"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.standby.per_proxy_latency_history = MagicMock(return_value=[
            ProxyLatencySample(1000.0, 0.2), ProxyLatencySample(1002.0, None)])
        self.server.per_proxy_traffic_history = MagicMock(return_value=[
            TrafficSample(1000.0, 10, 20), TrafficSample(1002.0, 30, 40)])
        resp = await self.client.get("/api/standby/series")
        assert resp.status == 200
        data = await resp.json()
        assert data["cursor"] == 1002.0
        # 窗口随响应下发（前端据此裁剪队列页本地缓冲，不硬编码）
        assert data["window"] == 60
        s = data["series"]["1.2.3.4:8080"]
        assert s["lat"] == [[1000.0, 0.2], [1002.0, None]]
        assert s["up"] == [[1000.0, 10], [1002.0, 30]]
        assert s["down"] == [[1000.0, 20], [1002.0, 40]]
        # 增量：窗口内无新点 → 序列为空、游标保持不变
        data2 = await (await self.client.get("/api/standby/series?since=1002")).json()
        assert data2["series"] == {}
        assert data2["cursor"] == 1002.0

    async def test_standby_series_includes_locked_proxy(self):
        """锁定代理已移出队列组，序列仍须下发（面板首行也要画图）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.move_proxy("http://1.2.3.4:8080", LOCKED_GROUP, consecutive_fails=0)
        self.standby.per_proxy_latency_history = MagicMock(
            return_value=[ProxyLatencySample(1000.0, 0.2)])
        data = await (await self.client.get("/api/standby/series")).json()
        assert list(data["series"]) == ["1.2.3.4:8080"]

    async def test_standby_series_skips_proxy_without_data(self):
        """既无延时也无流量的代理不下发空序列（省 payload）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        data = await (await self.client.get("/api/standby/series")).json()
        assert data["series"] == {}
        assert data["cursor"] == 0.0

    async def test_standby_series_invalid_since(self):
        resp = await self.client.get("/api/standby/series?since=abc")
        assert resp.status == 400

    async def test_standby_geo_from_cache(self):
        """队列归属地按 bind 下发（前端行内展示国家/城市）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.geo.query_cache = MagicMock(return_value=(
            {"1.2.3.4": {"country": "美国", "country_code": "US",
                         "province": "加州", "city": "洛杉矶", "lat": 34.0, "lng": -118.0}}, []))
        resp = await self.client.get("/api/standby/geo")
        assert resp.status == 200
        geo = (await resp.json())["geo"]
        assert geo["1.2.3.4:8080"] == {"country": "美国", "country_code": "US",
                                       "province": "加州", "city": "洛杉矶"}

    async def test_standby_geo_missing_triggers_locate(self):
        """缓存未命中 → executor 定位并落缓存；未解析出的 IP 下发 None"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:8080", consecutive_fails=0)
        self.ctx.geo.query_cache = MagicMock(return_value=({}, ["1.2.3.4", "5.6.7.8"]))
        self.ctx.geo.locate = MagicMock(return_value={
            "1.2.3.4": {"country": "日本", "country_code": "JP", "province": None, "city": "东京"}})
        data = await (await self.client.get("/api/standby/geo")).json()
        self.ctx.geo.locate.assert_called_once_with(["1.2.3.4", "5.6.7.8"])
        self.ctx.geo.save_cache.assert_called_once()
        assert data["geo"]["1.2.3.4:8080"]["city"] == "东京"
        assert data["geo"]["5.6.7.8:8080"] is None

    async def test_standby_geo_locate_failure_does_not_break(self):
        """归属定位异常时返回空映射（面板归位列显示 -，不影响其余功能）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.geo.query_cache = MagicMock(side_effect=RuntimeError("mmdb 挂了"))
        resp = await self.client.get("/api/standby/geo")
        assert resp.status == 200
        assert (await resp.json())["geo"] == {"1.2.3.4:8080": None}

    async def test_status_with_purity_cache(self):
        """status 快照带纯净度缓存数据（历史有数据时前端直接显示 tag，无需手动点击）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity.query_cache = MagicMock(return_value={
            "score": 35,
            "usage_type": "hosting",
            "from_cache": True,
        })
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby"][0]["purity"] == {"score": 35, "usage_type": "hosting"}

    async def test_status_carries_purity_source(self):
        """status 下发检查源元数据（前端据此显示/隐藏检查按钮并标注来源）"""
        assert (await (await self.client.get("/api/status")).json())["purity_source"] == {
            "name": "abuseipdb", "label": "AbuseIPDB",
        }
        self.ctx.purity = None  # 模拟纯净度禁用
        assert (await (await self.client.get("/api/status")).json())["purity_source"] is None

    async def test_purity_disabled_returns_404(self):
        """纯净度未启用（web 关闭/缺密钥/source 为空）：检查接口返回 404"""
        self.ctx.purity = None
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 404

    async def test_status_purity_none_when_no_cache(self):
        """无缓存时 purity 字段为 None"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        # query_cache 默认返回 None（make_ctx 设置）
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby"][0]["purity"] is None

    async def test_status_standby_entry_fields(self):
        """队列行带面板展示字段（入队时间/固定态），死字段不下发"""
        self.pool.add_to_group(STANDBY_GROUP, "socks5://1.2.3.4:1080",
                               consecutive_fails=0, latency=0.5)
        row = (await (await self.client.get("/api/status")).json())["standby"][0]
        assert row["pinned"] is False
        assert row["joined_at"] is not None
        # source_group 不下发：move_proxy 不保留它，队列行取值恒为空（发死字段无意义）
        assert "source_group" not in row
        # protocol 同理：前端不展示，scheme 已在 proxy 文本里
        assert "protocol" not in row

    async def test_status_locked_row_first_and_deduped(self):
        """锁定代理置首；队列组留有历史副本时不重复出行（前端以 proxy 为 :key）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:8080", consecutive_fails=0)
        self.pool.move_proxy("http://1.2.3.4:8080", LOCKED_GROUP, consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        data = await (await self.client.get("/api/status")).json()
        assert [r["proxy"] for r in data["standby"]] == ["http://1.2.3.4:8080", "http://5.6.7.8:8080"]
        assert [r["locked"] for r in data["standby"]] == [True, False]
        assert data["locked_proxy"] == "http://1.2.3.4:8080"

    async def test_logs_endpoint(self):
        """日志端点：全量拉取 / since 增量过滤 / entry 结构契约"""
        buf = logbuffer.get_buffer()
        base_seq = buf.snapshot()[1]  # 基准游标（单例跨测试共享，seq 不清零，按相对值断言）
        buf.emit(logging.LogRecord("src.use_cases.demo", logging.INFO, __file__, 1, "hello", (), None))
        buf.emit(logging.LogRecord("src.framework.serve", logging.DEBUG, __file__, 2, "detail", (), None))
        resp = await self.client.get("/api/logs")
        assert resp.status == 200
        data = await resp.json()
        assert data["cursor"] == base_seq + 2
        assert data["buffer_start"] >= 1  # 最旧条目 seq（缓冲非空）
        assert [e["seq"] for e in data["entries"]] == [base_seq + 1, base_seq + 2]
        assert data["entries"][0]["tag"] == "src.use_cases.demo"
        assert data["entries"][0]["level"] == 1
        assert data["entries"][0]["stack"] is None  # 无异常记录 stack=None
        assert isinstance(data["entries"][0]["ts"], int)
        # since 增量：只返回游标之后
        resp2 = await self.client.get(f"/api/logs?since={base_seq + 1}")
        data2 = await resp2.json()
        assert [e["seq"] for e in data2["entries"]] == [base_seq + 2]
        assert data2["cursor"] == base_seq + 2

    async def test_logs_since_invalid(self):
        """since 非法（负数/非数字）→ 400"""
        for bad in ("-1", "abc", "1.5"):
            resp = await self.client.get(f"/api/logs?since={bad}")
            assert resp.status == 400, f"since={bad} 应返回 400"

    async def test_logs_clear(self):
        """清空缓冲：GET 全量为空，cursor 保持单调（seq 不重置）"""
        buf = logbuffer.get_buffer()
        buf.clear()
        buf.emit(logging.LogRecord("src.x", logging.WARNING, __file__, 1, "w", (), None))
        resp = await self.client.post("/api/logs/clear")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        resp2 = await self.client.get("/api/logs")
        data = await resp2.json()
        assert data["entries"] == []

    async def test_group_detail_and_404(self):
        self.pool.add_to_group("trusted", "5.6.7.8:3128", consecutive_fails=0, latency=0.2)
        resp = await self.client.get("/api/group/trusted")
        assert resp.status == 200
        data = await resp.json()
        assert data["group"] == "trusted" and data["count"] == 1
        assert data["proxies"][0]["proxy"] == "http://5.6.7.8:3128"
        notfound = await self.client.get("/api/group/no-such-group")
        assert notfound.status == 404

    async def test_group_detail_geo_from_cache(self):
        """代理组明细带 IP 归属：缓存命中直接下发 geo 字段"""
        self.pool.add_to_group("trusted", "5.6.7.8:3128", consecutive_fails=0)
        self.ctx.geo.query_cache = MagicMock(return_value=(
            {"5.6.7.8": {"country": "美国", "country_code": "US", "province": "California",
                         "city": "Los Angeles", "lat": 34.05, "lng": -118.24, "cached_at": 1}},
            [],
        ))
        resp = await self.client.get("/api/group/trusted")
        data = await resp.json()
        geo = data["proxies"][0]["geo"]
        assert geo["country"] == "美国" and geo["country_code"] == "US"
        assert geo["city"] == "Los Angeles"

    async def test_group_detail_geo_missing_triggers_locate(self):
        """缓存缺失：locate 在 executor 执行并写缓存，结果合并下发"""
        self.pool.add_to_group("trusted", "5.6.7.8:3128", consecutive_fails=0)
        self.ctx.geo.query_cache = MagicMock(return_value=({}, ["5.6.7.8"]))
        self.ctx.geo.locate = MagicMock(return_value={
            "5.6.7.8": {"country": "日本", "country_code": "JP", "province": "Tokyo", "city": "Tokyo",
                        "lat": 35.68, "lng": 139.69},
        })
        resp = await self.client.get("/api/group/trusted")
        data = await resp.json()
        assert data["proxies"][0]["geo"]["country"] == "日本"
        self.ctx.geo.locate.assert_called_once_with(["5.6.7.8"])
        self.ctx.geo.save_cache.assert_called_once()

    async def test_fetch_control(self):
        """全量拉取：plan/collect/apply 三段编排，只有 collect 走线程（不碰池）"""
        resp = await self.client.post("/api/fetch", json={})
        assert resp.status == 200
        self.fetch.plan.assert_called_once_with(None)
        self.fetch.collect.assert_called_once()
        self.fetch.apply.assert_called_once()
        assert self.ctx.tasks.fetching_all() is False

    async def test_fetch_single_source(self):
        """单源拉取：body.source 透传给用例，只拉该源"""
        resp = await self.client.post("/api/fetch", json={"source": "src.interfaces.sources.demo"})
        assert resp.status == 200
        self.fetch.plan.assert_called_once_with("src.interfaces.sources.demo")

    async def test_fetch_rejected_while_running(self):
        """并发拉取幂等拒绝：已有拉取在跑时返回 running，且不再起第二个拉取"""
        try:
            assert self.ctx.tasks.fetch_start("src.interfaces.sources.demo") is True
            resp = await self.client.post("/api/fetch", json={})
            assert resp.status == 200
            assert (await resp.json())["status"] == "running"
            self.fetch.plan.assert_not_called()
        finally:
            self.ctx.tasks.fetch_done()

    async def test_fetch_state_exposed_and_cleared(self):
        """拉取中间态：进行中随快照下发（刷新可恢复），结束后清除"""
        src = SOURCES[0]["name"]
        assert self.ctx.tasks.fetch_start(src) is True
        data = await (await self.client.get("/api/status")).json()
        assert data["fetching_all"] is False
        assert [s["fetching"] for s in data["sources"] if s["name"] == src] == [True]
        self.ctx.tasks.fetch_done()
        data = await (await self.client.get("/api/status")).json()
        assert all(s["fetching"] is False for s in data["sources"])

    async def test_blacklist_removes_proxy(self):
        self.pool.add_to_group("default-http", "9.9.9.9:80", consecutive_fails=0)
        resp = await self.client.post("/api/blacklist", json={"proxies": ["9.9.9.9:80"]})
        assert resp.status == 200
        assert (await resp.json())["count"] == 1
        assert "9.9.9.9:80" not in self.pool.get_available_proxies()

    async def test_blacklist_syncs_pproxy(self):
        """拉黑后同步 pproxy 上游（standby 队列变化时）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        resp = await self.client.post("/api/blacklist", json={"proxies": ["1.2.3.4:8080"]})
        assert resp.status == 200
        self.server.sync.assert_called_once()

    async def test_proxy_move(self):
        """移出代理到目标组：从 standby 移到 trusted，同步 pproxy"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        resp = await self.client.post("/api/proxy/move", json={
            "proxies": ["http://1.2.3.4:8080"], "target_group": TRUSTED_GROUP,
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["target_group"] == TRUSTED_GROUP
        assert "http://1.2.3.4:8080" in self.pool.get_trusted_proxies()
        self.server.sync.assert_called_once()

    async def test_proxy_move_batch(self):
        """批量移动：proxies 数组全部移到目标组，一次 sync + save"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:3128", consecutive_fails=0)
        resp = await self.client.post("/api/proxy/move", json={
            "proxies": ["http://1.2.3.4:8080", "http://5.6.7.8:3128"],
            "target_group": TRUSTED_GROUP,
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["count"] == 2
        assert "http://1.2.3.4:8080" in self.pool.get_trusted_proxies()
        assert "http://5.6.7.8:3128" in self.pool.get_trusted_proxies()
        self.server.sync.assert_called_once()
        self.ctx.save.assert_called()

    async def test_proxy_move_missing_params(self):
        """缺少 proxies 或 target_group 返回 400"""
        r1 = await self.client.post("/api/proxy/move", json={"proxies": ["1.2.3.4:80"]})
        assert r1.status == 400
        r2 = await self.client.post("/api/proxy/move", json={"target_group": "trusted"})
        assert r2.status == 400

    async def test_proxy_move_unknown_group(self):
        """目标组不存在返回 400"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        r = await self.client.post("/api/proxy/move", json={
            "proxies": ["http://1.2.3.4:8080"], "target_group": "no-such-group",
        })
        assert r.status == 400

    async def test_trust_adds_proxy(self):
        resp = await self.client.post("/api/trust", json={"proxies": ["7.7.7.7:99"]})
        assert resp.status == 200
        assert "http://7.7.7.7:99" in self.pool.get_trusted_proxies()

    async def test_source_pause_resume(self):
        pool = self.pool
        r = await self.client.post("/api/source/pause",
                                   json={"source": "https://example.com/list.txt", "ttl": 60})
        assert r.status == 200
        assert "https://example.com/list.txt" in pool.source_paused
        r2 = await self.client.post("/api/source/resume",
                                    json={"source": "https://example.com/list.txt"})
        assert r2.status == 200
        assert "https://example.com/list.txt" not in pool.source_paused
        bad = await self.client.post("/api/source/pause", json={})
        assert bad.status == 400

    async def test_settings_update_side_effects(self):
        resp = await self.client.post("/api/settings", json={
            "interval": 120, "salgorithm": "lc",
            "fill_probe_multiplier": 8, "allowed_types": ["http"],
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["settings"]["interval"] == 120
        assert data["settings"]["salgorithm"] == "lc"
        self.server.set_salgorithm.assert_awaited_once_with("lc")
        assert (8, STANDBY_FILL_PROBE_MIN_EXTRA) in self.standby.probe_params

    async def test_settings_invalid_rejected(self):
        resp = await self.client.post("/api/settings", json={"interval": 0})
        assert resp.status == 400
        resp2 = await self.client.post("/api/settings", json={"salgorithm": "bogus"})
        assert resp2.status == 400

    async def test_standby_pin_unpin(self):
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        r = await self.client.post("/api/standby/pin", json={"proxy": "http://1.2.3.4:8080"})
        assert r.status == 200
        assert (await r.json())["pinned"] is True
        assert self.pool.is_pinned("http://1.2.3.4:8080")
        # status 返回 pinned 字段
        resp = await self.client.get("/api/status")
        data = await resp.json()
        assert data["standby"][0]["pinned"] is True
        # unpin
        r2 = await self.client.post("/api/standby/unpin", json={"proxy": "http://1.2.3.4:8080"})
        assert r2.status == 200
        assert not self.pool.is_pinned("http://1.2.3.4:8080")

    async def test_standby_pin_rejects_non_standby(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        r = await self.client.post("/api/standby/pin", json={"proxy": "http://1.2.3.4:8080"})
        assert r.status == 400

    async def test_standby_pin_missing_proxy(self):
        r = await self.client.post("/api/standby/pin", json={})
        assert r.status == 400

    async def test_standby_lock_unlock(self):
        """lock 代理为唯一上游，status 返回 locked_proxy；unlock 后恢复"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:3128", consecutive_fails=0)
        r = await self.client.post("/api/standby/lock", json={"proxy": "http://1.2.3.4:8080"})
        assert r.status == 200
        assert (await r.json())["locked"] is True
        # status 返回 locked_proxy + standby 首行为锁定代理（locked=true，已移出队列组故需合并下发）
        resp = await self.client.get("/api/status")
        data = await resp.json()
        assert data["locked_proxy"] == "http://1.2.3.4:8080"
        assert [(p["proxy"], p["locked"]) for p in data["standby"]] == [
            ("http://1.2.3.4:8080", True),
            ("http://5.6.7.8:3128", False),
        ]
        # LOCKED_GROUP 不在 groups 列表（隐藏）
        assert "_proxy-pool-locked" not in data["groups"]
        # unlock
        r2 = await self.client.post("/api/standby/unlock", json={})
        assert r2.status == 200
        assert (await r2.json())["locked"] is False
        resp2 = await self.client.get("/api/status")
        data2 = await resp2.json()
        assert data2["locked_proxy"] is None
        assert set(p["proxy"] for p in data2["standby"]) == {"http://1.2.3.4:8080", "http://5.6.7.8:3128"}
        assert not any(p["locked"] for p in data2["standby"])

    async def test_standby_lock_rejects_non_standby(self):
        """lock 非队列代理返回 400"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        r = await self.client.post("/api/standby/lock", json={"proxy": "http://1.2.3.4:8080"})
        assert r.status == 400

    async def test_standby_lock_missing_proxy(self):
        r = await self.client.post("/api/standby/lock", json={})
        assert r.status == 400

    async def test_proxy_history_missing_bind(self):
        """缺少 bind 参数返回 400"""
        r = await self.client.get("/api/proxy/history")
        assert r.status == 400

    async def test_proxy_history_returns_empty_for_unknown_bind(self):
        """未知 bind 返回空历史列表"""
        r = await self.client.get("/api/proxy/history?bind=9.9.9.9:9999")
        assert r.status == 200
        data = await r.json()
        assert data["bind"] == "9.9.9.9:9999"
        assert data["latency_history"] == []
        assert data["traffic_history"] == []

    async def test_speedtest_missing_bind(self):
        """缺少 bind 参数返回 400"""
        r = await self.client.post("/api/proxy/speedtest", json={})
        assert r.status == 400

    async def test_speedtest_unknown_bind(self):
        """未知 bind 返回 404"""
        r = await self.client.post("/api/proxy/speedtest", json={"bind": "9.9.9.9:9999"})
        assert r.status == 404

    async def test_speedtest_runs(self):
        """正常测速：mock pspeed probe_one，验证端点返回精简结果"""
        from unittest.mock import patch
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        fake_result = {
            "proxy": "http://1.2.3.4:8080", "ok": True, "tcp": None, "ip": None,
            "download": {"ok": True, "best_mbps": 5.0, "avg_mbps": 4.0, "bytes": 1000000,
                         "ms": 5000.0, "node": "cloudflare", "err": None},
            "upload": {"ok": True, "best_mbps": 2.0, "avg_mbps": 1.5, "bytes": 500000,
                       "ms": 5000.0, "node": "cloudflare", "err": None},
            "error": None,
        }
        with patch("pspeed.probe.probe_one", return_value=fake_result):
            r = await self.client.post("/api/proxy/speedtest", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["proxy"] == "http://1.2.3.4:8080"
        assert data["download"]["best_mbps"] == 5.0
        assert data["upload"]["best_mbps"] == 2.0

    async def test_speedtest_running_idempotent(self):
        """测速进行中重复请求返回 {"status":"running"}，不重复起任务"""
        from unittest.mock import patch
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        assert self.ctx.tasks.speedtest_start("1.2.3.4:8080") is True
        with patch("pspeed.probe.probe_one") as probe_mock:
            r = await self.client.post("/api/proxy/speedtest", json={"bind": "1.2.3.4:8080"})
            probe_mock.assert_not_called()
        assert r.status == 200
        assert (await r.json()) == {"status": "running"}

    async def test_status_carries_speedtest_state(self):
        """status 快照带测速任务状态（前端刷新后恢复测速中/结果）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.tasks.speedtest_done("1.2.3.4:8080", {"proxy": "http://1.2.3.4:8080"})
        row = (await (await self.client.get("/api/status")).json())["standby"][0]
        assert row["speedtest"]["status"] == "done"
        assert row["speedtest"]["result"]["proxy"] == "http://1.2.3.4:8080"
        assert row["speedtest"]["ts"] is not None

    async def test_status_speedtest_none_when_no_record(self):
        """无测速记录时 speedtest 字段为 None"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        row = (await (await self.client.get("/api/status")).json())["standby"][0]
        assert row["speedtest"] is None
        assert row["purity_state"] is None

    async def test_group_add_proxies(self):
        """向组批量添加代理"""
        r = await self.client.post("/api/group/trusted/add", json={"proxies": ["3.3.3.3:80", "http://4.4.4.4:3128"]})
        assert r.status == 200
        data = await r.json()
        assert data["added"] == 2
        assert "http://3.3.3.3:80" in self.pool.get_trusted_proxies()

    async def test_group_add_empty(self):
        """空列表返回 400"""
        r = await self.client.post("/api/group/trusted/add", json={"proxies": []})
        assert r.status == 400

    async def test_group_add_unknown_group(self):
        """不存在的组返回 404"""
        r = await self.client.post("/api/group/no-such-group/add", json={"proxies": ["1.2.3.4:80"]})
        assert r.status == 404

    async def test_proxy_remove(self):
        """从指定组删除代理"""
        self.pool.add_to_group("default-http", "http://9.9.9.9:80", consecutive_fails=0)
        r = await self.client.post("/api/proxy/remove", json={"proxies": ["http://9.9.9.9:80"], "group": "default-http"})
        assert r.status == 200
        assert "http://9.9.9.9:80" not in self.pool.get_available_proxies()

    async def test_proxy_remove_batch(self):
        """批量删除：proxies 数组全部从指定组移除，返回实际删除数"""
        self.pool.add_to_group("default-http", "http://9.9.9.9:80", consecutive_fails=0)
        self.pool.add_to_group("default-http", "http://8.8.8.8:8080", consecutive_fails=0)
        r = await self.client.post("/api/proxy/remove", json={
            "proxies": ["http://9.9.9.9:80", "http://8.8.8.8:8080", "http://0.0.0.0:1"],
            "group": "default-http",
        })
        assert r.status == 200
        data = await r.json()
        assert data["count"] == 2
        assert "http://9.9.9.9:80" not in self.pool.get_available_proxies()
        assert "http://8.8.8.8:8080" not in self.pool.get_available_proxies()

    async def test_proxy_remove_missing_params(self):
        """缺少 proxies 或 group 返回 400"""
        r1 = await self.client.post("/api/proxy/remove", json={"proxies": ["1.2.3.4:80"]})
        assert r1.status == 400
        r2 = await self.client.post("/api/proxy/remove", json={"group": "default-http"})
        assert r2.status == 400

    async def test_proxy_remove_not_in_group(self):
        """代理不在该组返回 404"""
        r = await self.client.post("/api/proxy/remove", json={"proxies": ["http://0.0.0.0:1"], "group": "default-http"})
        assert r.status == 404

    # ---------------- IP 纯净度检查 ----------------
    async def test_purity_missing_bind(self):
        """缺少 bind 返回 400"""
        self.ctx.purity = make_purity_mock()
        r = await self.client.post("/api/proxy/purity", json={})
        assert r.status == 400

    async def test_purity_proxy_not_found(self):
        """代理不在队列/锁定中返回 404"""
        self.ctx.purity = make_purity_mock()
        r = await self.client.post("/api/proxy/purity", json={"bind": "0.0.0.0:1"})
        assert r.status == 404

    async def test_purity_success(self):
        """缓存未命中 → 调检查源 → 写缓存 → 返回结果"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        self.ctx.purity.check_remote = MagicMock(return_value={
            "ip": "1.2.3.4",
            "score": 3,
            "usage_type": "hosting",
            "extra": {"country_code": "US"},
        })
        self.ctx.purity.save_cache = MagicMock(side_effect=lambda proxy, result: result)
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["score"] == 3
        assert data["usage_type"] == "hosting"
        assert data["ip"] == "1.2.3.4"

    async def test_purity_save_cache_error_returns_500(self):
        """写缓存失败返回 500（结果不落地，前端提示失败）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        self.ctx.purity.check_remote = MagicMock(return_value={"score": 3, "usage_type": "hosting"})
        self.ctx.purity.save_cache = MagicMock(side_effect=RuntimeError("db locked"))
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 500
        assert self.ctx.tasks.purity_of("1.2.3.4")["status"] == "error"

    async def test_purity_cache_hit(self):
        """缓存命中：直接返回 from_cache=True，不调 check_remote"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        self.ctx.purity.query_cache = MagicMock(return_value={
            "ip": "1.2.3.4",
            "score": 0,
            "usage_type": "residential",
            "from_cache": True,
        })
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["from_cache"] is True
        self.ctx.purity.check_remote.assert_not_called()

    async def test_purity_checker_error_returns_500(self):
        """检查源抛异常返回 500"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        self.ctx.purity.check_remote = MagicMock(side_effect=RuntimeError("API error"))
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 500

    async def test_purity_checking_idempotent(self):
        """检查进行中重复请求返回 {"status":"checking"}，不重复调 API（防日限额）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        assert self.ctx.tasks.purity_start("1.2.3.4") is True
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        assert (await r.json()) == {"status": "checking"}
        self.ctx.purity.check_remote.assert_not_called()

    async def test_purity_checking_shared_same_ip(self):
        """同 IP 不同端口行共享 checking：第二个端口请求直接返回 checking"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8081", consecutive_fails=0)
        self.ctx.purity = make_purity_mock()
        assert self.ctx.tasks.purity_start("1.2.3.4") is True
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8081"})
        assert (await r.json()) == {"status": "checking"}
        self.ctx.purity.check_remote.assert_not_called()

    async def test_status_carries_purity_state(self):
        """status 快照带纯净度检查中间态（前端刷新后恢复检查中/失败）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.tasks.purity_start("1.2.3.4")
        row = (await (await self.client.get("/api/status")).json())["standby"][0]
        assert row["purity_state"]["status"] == "checking"


class WebApiGeoDisabledTest(unittest.IsolatedAsyncioTestCase):
    """geoip.enabled=false 场景：geo=None 时不注册地图/归属路由、归属字段恒为 null"""

    async def asyncSetUp(self):
        self.pool = make_pool()
        self.ctx, self.fetch, self.standby, self.server = make_ctx(self.pool)
        self.ctx.geo = None  # 模拟 geoip.enabled=false
        self.client = TestClient(TestServer(build_app(self.ctx)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_geo_map_route_not_registered(self):
        """地图路由未注册：/api/geo/map 返回 404"""
        resp = await self.client.get("/api/geo/map")
        assert resp.status == 404

    async def test_standby_geo_route_not_registered(self):
        """队列归属路由未注册：/api/standby/geo 返回 404（前端按 geo_enabled 跳过）"""
        resp = await self.client.get("/api/standby/geo")
        assert resp.status == 404

    async def test_status_geo_enabled_false(self):
        """status 快照 geo_enabled=false，前端据此隐藏地图区块"""
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["geo_enabled"] is False

    async def test_group_detail_geo_is_null(self):
        """组明细的 geo 恒为 null（归属查询跳过）"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=0)
        resp = await self.client.get("/api/group/default-http")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["proxies"]) == 1
        assert data["proxies"][0]["geo"] is None


if __name__ == "__main__":
    unittest.main()
