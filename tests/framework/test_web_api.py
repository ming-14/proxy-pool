"""Web 控制面板 API 测试：注入 fake container/standby/server，覆盖读与写各端点

保持工程测试惯例：真实 ProxyPool 作为数据后端，容器组件（fetch/standby/
server）用 mock，隔离网络与 pproxy。用 aiohttp TestClient 直接驱动路由。
"""

import asyncio
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from aiohttp.test_utils import TestClient, TestServer

from src.domain.proxy_pool import ProxyPool, LOCKED_GROUP
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework.config import (
    PROXY_GROUPS,
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
    STANDBY_GROUP,
    STANDBY_FILL_PROBE_MULTIPLIER,
    STANDBY_FILL_PROBE_MIN_EXTRA,
)
from src.framework.web.control import ServeControl
from src.framework.web.api import build_app, PanelContext


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


def make_ctx(pool):
    fetch = MagicMock()
    record = RecordProxyUseCase(pool=pool, standby_group=STANDBY_GROUP)
    standby = FakeStandby(pool)
    server = MagicMock()
    server.sync = MagicMock()
    server.set_salgorithm = AsyncMock()
    server.traffic_history = MagicMock(return_value=[])
    server.traffic_total = MagicMock(return_value=(0, 0))
    server.per_proxy_traffic = MagicMock(return_value={})
    server.per_proxy_traffic_history = MagicMock(return_value=[])
    control = ServeControl(
        interval=60, concurrency=4, timeout=5, probe_url="https://probe.local/ip",
        allowed_types={"http", "socks5"},
        fill_probe_multiplier=STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=STANDBY_FILL_PROBE_MIN_EXTRA,
    )
    purity = MagicMock()
    purity.query_cache = MagicMock(return_value=None)  # 无缓存，_status_snapshot 里 _purity_cached 返回 None
    geo = MagicMock()
    # IP 归属：默认无缓存、无定位结果（group detail 正常路径，避免解包异常被吞）
    geo.query_cache = MagicMock(return_value=({}, []))
    geo.locate = MagicMock(return_value={})
    geo.save_cache = MagicMock()
    ctx = PanelContext(pool=pool, fetch=fetch, record=record, standby=standby,
                       control=control, server=server, save=MagicMock(), capacity=5,
                       purity=purity, geo=geo)
    return ctx, fetch, standby, server


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
        # since 过滤：无晚于游标的数据返回空
        resp2 = await self.client.get("/api/status/history?since=9999999999")
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["standby_history"] == []
        assert data2["traffic_history"] == []

    async def test_status_with_purity_cache(self):
        """status 快照带 purity 缓存数据（历史有数据时前端直接显示 tag，无需手动点击）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity.query_cache = MagicMock(return_value={
            "abuse_confidence_score": 35,
            "usage_type": "hosting",
            "from_cache": True,
        })
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby"][0]["purity"] == {"abuse_confidence_score": 35, "usage_type": "hosting"}

    async def test_status_purity_none_when_no_cache(self):
        """无缓存时 purity 字段为 None"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        # query_cache 默认返回 None（make_ctx 设置）
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["standby"][0]["purity"] is None

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
        resp = await self.client.post("/api/fetch", json={})
        assert resp.status == 200
        self.fetch.execute.assert_called_once()

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
        # status 返回 locked_proxy + standby 含完整队列（锁定代理已移出 standby 组）
        resp = await self.client.get("/api/status")
        data = await resp.json()
        assert data["locked_proxy"] == "http://1.2.3.4:8080"
        assert [p["proxy"] for p in data["standby"]] == ["http://5.6.7.8:3128"]
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
        with patch("src.framework.web.api._pspeed_probe", return_value=fake_result):
            r = await self.client.post("/api/proxy/speedtest", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["proxy"] == "http://1.2.3.4:8080"
        assert data["download"]["best_mbps"] == 5.0
        assert data["upload"]["best_mbps"] == 2.0

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

    # ---------------- AbuseIPDB 纯洁度检查 ----------------
    async def test_purity_missing_bind(self):
        """缺少 bind 返回 400"""
        self.ctx.purity = MagicMock()
        r = await self.client.post("/api/proxy/purity", json={})
        assert r.status == 400

    async def test_purity_proxy_not_found(self):
        """代理不在队列/锁定中返回 404"""
        self.ctx.purity = MagicMock()
        r = await self.client.post("/api/proxy/purity", json={"bind": "0.0.0.0:1"})
        assert r.status == 404

    async def test_purity_success(self):
        """缓存未命中 → 调 API → 写缓存 → 返回结果"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = MagicMock()
        self.ctx.purity.query_cache = MagicMock(return_value=None)
        self.ctx.purity.check_remote = MagicMock(return_value={
            "ip": "1.2.3.4",
            "abuse_confidence_score": 3,
            "usage_type": "Data Center/Web Hosting/Transit",
            "country_code": "US",
        })
        self.ctx.purity.save_cache = MagicMock(side_effect=lambda proxy, result: result)
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["abuse_confidence_score"] == 3
        assert data["usage_type"] == "Data Center/Web Hosting/Transit"
        assert data["ip"] == "1.2.3.4"

    async def test_purity_cache_hit(self):
        """缓存命中：直接返回 from_cache=True，不调 check_remote"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = MagicMock()
        self.ctx.purity.query_cache = MagicMock(return_value={
            "ip": "1.2.3.4",
            "abuse_confidence_score": 0,
            "usage_type": "Fixed Line ISP",
            "from_cache": True,
        })
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 200
        data = await r.json()
        assert data["from_cache"] is True
        self.ctx.purity.check_remote.assert_not_called()

    async def test_purity_checker_error_returns_500(self):
        """checker 抛异常返回 500"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.ctx.purity = MagicMock()
        self.ctx.purity.query_cache = MagicMock(return_value=None)
        self.ctx.purity.check_remote = MagicMock(side_effect=RuntimeError("API error"))
        r = await self.client.post("/api/proxy/purity", json={"bind": "1.2.3.4:8080"})
        assert r.status == 500


class WebApiGeoDisabledTest(unittest.IsolatedAsyncioTestCase):
    """geoip.enabled=false 场景：geo=None 时不注册地图路由、组明细不带 geo 字段"""

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

    async def test_status_geo_enabled_false(self):
        """status 快照 geo_enabled=false，前端据此隐藏地图区块"""
        resp = await self.client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["geo_enabled"] is False

    async def test_group_detail_without_geo_field(self):
        """组明细不带 geo 字段（归属查询跳过）"""
        resp = await self.client.get("/api/group/default-http")
        assert resp.status == 200
        data = await resp.json()
        for p in data["proxies"]:
            assert "geo" not in p


if __name__ == "__main__":
    unittest.main()
