"""DNS 探测队列 e2e：有流量（入队）即探测 → 缓存落盘 → /api/globe 读到目标弧

覆盖要点：
1. 入队即探测（事件驱动）：只发一次目标请求、**不发任何 /api/globe 请求**，后台立刻
   穿该代理做 DoH（mock 上游）→ dns_probe_cache 出现该 (bind, host) 行；
2. 3D 地球只消费最终数据：随后请求 /api/globe 即可读到上一步落盘的目标弧（地球不驱动探测）；
3. [dns_probe].enabled=false：不构造队列，流量不触发任何探测、缓存不新增行。

全程本地 mock：mock 上游同时扮演被选中的代理与 DoH 端点（返回 DoH JSON）；
GeoIP 用假定位器（本测试的范围是探测链路、serve 接线与 API，不是 GeoLite2）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

import aiohttp

from src.domain.proxy_pool import ProxyPool
from src.framework import config
from src.framework.serve import _run
from src.interfaces.http_client import AsyncHttpGetter
from src.interfaces.proxy_server import PproxyServer
from src.interfaces.sqlite_store import SqliteProxyStore
from src.use_cases.enqueue_filter import EnqueueFilterUseCase
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.standby import StandbyQueueUseCase

from tests.e2e.mock_upstream import start_mock_upstream

# DoH JSON 响应：CNAME 在前 + A 记录——解析器须忽略非 A 记录
DOH_BODY = json.dumps({
    "Status": 0,
    "Answer": [
        {"name": "example.com", "type": 5, "data": "cdn.example.net"},
        {"name": "cdn.example.net", "type": 1, "data": "8.8.4.4"},
    ],
}).encode()

# 目标请求的 host 与上面 DoH 响应配套（穿代理解析出 8.8.4.4）
TARGET_HOST = "example.com"
TARGET_IP = "8.8.4.4"
TARGET_GEO = {"country": "美国", "country_code": "US", "province": None,
              "city": "Ashburn", "lat": 39.0, "lng": -77.5}


async def close_server(srv):
    """关闭 asyncio.Server（close 返回 None，非 awaitable）"""
    srv.close()
    await srv.wait_closed()


def free_local_port() -> int:
    """取一个刚释放的本地端口（Web 面板用；代理监听用 0 让内核分配后就地读取）"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_pool():
    return ProxyPool(
        config.PROXY_GROUPS,
        max_consecutive_fails=config.MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=config.TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=config.SOURCE_PAUSE_TTL,
        requestable_groups=config.REQUESTABLE_GROUPS,
        trusted_group=config.TRUSTED_GROUP,
        blacklist_group=config.BLACKLIST_GROUP,
    )


def standby_list(pool):
    """代理服务队列组当前代理"""
    return list(pool.proxy_groups[config.STANDBY_GROUP]["proxies"].keys())


class _FakeCheck:
    """可控探测器：alive 集合内的成功（fill 只把探测成功的代理补入队列）"""

    def __init__(self, alive_proxies):
        self.alive = set(alive_proxies)

    async def execute(self, proxies, url, **kw):
        results = {
            p: ({"success": True, "status_code": 200, "elapsed": 0.1, "error": None}
                if p in self.alive else
                {"success": False, "status_code": None, "elapsed": 1, "error": "Timeout"})
            for p in proxies
        }
        on_result = kw.get("on_result")
        if on_result:
            for p in proxies:
                ret = on_result(p, results[p])
                if inspect.isawaitable(ret):
                    await ret
        return results


def make_standby(pool, record, check):
    """代理服务队列用例（容器壳）：网络门控注入恒可用的 mock"""
    gate = MagicMock()

    async def _gate_ok():
        return True

    gate.check.side_effect = _gate_ok
    return StandbyQueueUseCase(
        pool=pool, record=record, check_proxies=check, network_gate=gate,
        enqueue_filter=EnqueueFilterUseCase(rules=config.ENQUEUE_RULES),
        standby_group=config.STANDBY_GROUP, reject_group=config.ENQUEUE_REJECT_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
        metrics_window=config.METRICS_WINDOW,
        chart_window=config.STANDBY_CHART_WINDOW,
    )


class FakeGeo:
    """假定位器：目标 IP → 固定城市（真实 GeoLite2 不在本测试范围）"""

    async def resolve(self, ips, *, label):
        return {ip: dict(TARGET_GEO) for ip in ips if ip == TARGET_IP}


@contextmanager
def _dns_probe_config(mock_port: int, *, enabled: bool = True):
    """把 DNS 探测关键项指到本地 mock：DoH 端点 = mock 上游；手填地球坐标便于断言 self"""
    with patch.object(config, "DNS_PROBE_ENABLED", enabled), \
            patch.object(config, "DNS_PROBE_DOH_ENDPOINTS",
                         (f"http://127.0.0.1:{mock_port}/dns-query",)), \
            patch.object(config, "GLOBE_ENABLED", True), \
            patch.object(config, "GLOBE_SELF_LAT", 31.2), \
            patch.object(config, "GLOBE_SELF_LNG", 121.5):
        yield


async def _wait_until(pred, timeout=10.0, what="条件"):
    """轮询等待（serve 内部流程异步启动）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} 等待超时")


async def _send_target_request(listen_port: int) -> bytes:
    """经本地 pproxy 发一次请求（记录 (bind=mock 上游, host=TARGET_HOST) 流量 pair）"""
    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    writer.write(f"GET http://{TARGET_HOST}/ HTTP/1.1\r\nHost: {TARGET_HOST}\r\n\r\n".encode())
    await writer.drain()
    resp = await reader.read(4096)
    writer.close()
    await writer.wait_closed()
    return resp


def _probe_rows(db_path: str) -> list[tuple]:
    """直接读 dns_probe_cache（WAL 模式允许外部连接读）"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT bind, host, ip FROM dns_probe_cache").fetchall()
    finally:
        conn.close()


async def _get_globe(url: str, timeout: float = 10.0) -> dict:
    """轮询 /api/globe 直到出现非空 flows（面板未就绪时重试；超时返回最后一帧）"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(url) as resp:
                    last = await resp.json()
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)
                continue
            if last.get("flows"):
                return last
            await asyncio.sleep(0.4)
    return last


@asynccontextmanager
async def _serve_running(mock_port: int, store, holder: dict, *, probe_enabled: bool = True):
    """启动 serve._run（真实 PproxyServer 监听 0、真实 store/getter、假 geo）并保证清理

    holder["server"] 是补丁捕获的真实 PproxyServer（serve 内部创建，测试需读实际
    监听端口）。配置补丁与 PproxyServer 补丁都要覆盖任务启动与整个运行期：
    _run 在首个 await 后才真正开跑，补丁提前退出会让捕获失效。
    """
    proxy = f"http://127.0.0.1:{mock_port}"
    pool = make_pool()
    pool.add_to_group("default-http", proxy)
    record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
    standby = make_standby(pool, record, _FakeCheck([proxy]))
    c = SimpleNamespace(
        pool=pool, standby=standby,
        save=lambda: None, save_full=lambda: None,
        async_getter=AsyncHttpGetter(), store=store, geo=FakeGeo(),
        purity=None, speedtest=MagicMock(), fetch=MagicMock(), record=record,
    )
    real_cls = PproxyServer

    def factory(*args, **kwargs):
        holder["server"] = real_cls(*args, **kwargs)
        return holder["server"]

    web_port = free_local_port()
    with _dns_probe_config(mock_port, enabled=probe_enabled), \
            patch("src.framework.serve.PproxyServer", new=factory):
        task = asyncio.create_task(_run(
            c, interval=30, probe_url="http://probe.local", allowed_types={"http"},
            concurrency=2, host="127.0.0.1", port=0, capacity=5,
            web_host="127.0.0.1", web_port=web_port, web_enabled=True,
        ))
        try:
            yield c.pool, web_port, proxy
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class DnsProbeE2E(unittest.TestCase):
    """serve 自带的 DNS 探测链路：流量入队 → 立刻穿代理 DoH → 缓存落盘 → 地球读到弧"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="proxy-pool-dns-probe-e2e-")
        self.db_path = os.path.join(self.tmp_dir, "cache.db")
        self.store = SqliteProxyStore(self.db_path)

    def tearDown(self):
        self.store.close()  # Windows 下先关连接再删目录
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_traffic_triggers_probe_without_globe_request(self):
        """只有流量（无任何 /api/globe 请求）→ 入队即探测 → 缓存落盘；地球随后读到该弧"""
        async def run():
            upstream, mock_port = await start_mock_upstream(body=DOH_BODY)
            holder: dict = {}
            try:
                async with _serve_running(mock_port, self.store, holder) as (pool, web_port, proxy):
                    await _wait_until(lambda: standby_list(pool) == [proxy], what="队列补入")
                    await _wait_until(lambda: holder["server"]._handler is not None, what="pproxy 监听")
                    listen_port = holder["server"]._handler.sockets[0].getsockname()[1]

                    resp = await _send_target_request(listen_port)
                    self.assertIn(b"200", resp, f"目标请求应经 mock 上游成功: {resp!r}")

                    # 关键断言：没有任何 /api/globe 请求，探测也必须发生并落盘
                    await _wait_until(lambda: _probe_rows(self.db_path), what="DNS 探测落盘")
                    rows = _probe_rows(self.db_path)
                    self.assertEqual(rows, [(f"127.0.0.1:{mock_port}", TARGET_HOST, TARGET_IP)])

                    # 3D 地球只消费最终数据：请求即读到上面落盘的目标弧
                    payload = await _get_globe(f"http://127.0.0.1:{web_port}/api/globe")
                    self.assertEqual(payload["self"], {"lat": 31.2, "lng": 121.5})
                    self.assertNotIn("stats", payload)
                    flows = payload["flows"]
                    self.assertTrue(flows, "地球应读到已落盘的目标弧")
                    flow = flows[0]
                    self.assertEqual(flow["bind"], f"127.0.0.1:{mock_port}")
                    self.assertEqual(flow["city"], "Ashburn")
                    self.assertEqual(flow["hosts"], [TARGET_HOST])
                    self.assertEqual((flow["lat"], flow["lng"]),
                                     (TARGET_GEO["lat"], TARGET_GEO["lng"]))
                    self.assertEqual([n["bind"] for n in payload["nodes"]],
                                     [f"127.0.0.1:{mock_port}"])
            finally:
                await close_server(upstream)

        asyncio.run(run())

    def test_disabled_probe_records_nothing(self):
        """[dns_probe].enabled=false：不构造队列，流量不触发任何探测"""
        async def run():
            upstream, mock_port = await start_mock_upstream(body=DOH_BODY)
            holder: dict = {}
            try:
                async with _serve_running(mock_port, self.store, holder,
                                          probe_enabled=False) as (pool, _web_port, proxy):
                    await _wait_until(lambda: standby_list(pool) == [proxy], what="队列补入")
                    await _wait_until(lambda: holder["server"]._handler is not None, what="pproxy 监听")
                    listen_port = holder["server"]._handler.sockets[0].getsockname()[1]
                    await _send_target_request(listen_port)

                    await asyncio.sleep(3.0)

                    self.assertEqual(_probe_rows(self.db_path), [], "探测关闭时不得落盘")
            finally:
                await close_server(upstream)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()