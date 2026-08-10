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

    def standby_proxies(self):
        # 完整队列（不因锁定状态改变，用于展示/指标）
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
    ctx = PanelContext(pool=pool, fetch=fetch, record=record, standby=standby,
                       control=control, server=server, save=MagicMock(), capacity=5)
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
        assert data["standby"][0]["proxy"] == "http://1.2.3.4:8080"
        assert data["settings"]["salgorithm"] == "rc"
        assert data["settings"]["allowed_types"] == ["http", "socks5"]
        assert any(s["name"] for s in data["sources"])
        assert "Proxy-pool-standby-queue" in data["groups"]
        assert data["standby_history"] == []
        assert data["traffic_history"] == []
        assert data["traffic_total"] == {"upload": 0, "download": 0}

    async def test_group_detail_and_404(self):
        self.pool.add_to_group("trusted", "5.6.7.8:3128", consecutive_fails=0, latency=0.2)
        resp = await self.client.get("/api/group/trusted")
        assert resp.status == 200
        data = await resp.json()
        assert data["group"] == "trusted" and data["count"] == 1
        assert data["proxies"][0]["proxy"] == "http://5.6.7.8:3128"
        notfound = await self.client.get("/api/group/no-such-group")
        assert notfound.status == 404

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
            "proxy": "http://1.2.3.4:8080", "target_group": TRUSTED_GROUP,
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["target_group"] == TRUSTED_GROUP
        assert "http://1.2.3.4:8080" in self.pool.get_trusted_proxies()
        self.server.sync.assert_called_once()

    async def test_proxy_move_missing_params(self):
        """缺少 proxy 或 target_group 返回 400"""
        r1 = await self.client.post("/api/proxy/move", json={"proxy": "1.2.3.4:80"})
        assert r1.status == 400
        r2 = await self.client.post("/api/proxy/move", json={"target_group": "trusted"})
        assert r2.status == 400

    async def test_proxy_move_unknown_group(self):
        """目标组不存在返回 400"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        r = await self.client.post("/api/proxy/move", json={
            "proxy": "http://1.2.3.4:8080", "target_group": "no-such-group",
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


if __name__ == "__main__":
    unittest.main()
