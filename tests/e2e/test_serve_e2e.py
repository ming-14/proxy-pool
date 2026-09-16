"""代理服务器 e2e 测试：真实 pproxy + 本地 mock 上游 + 完整业务流

覆盖：
1. serve 启动：代理服务队列补入 + pproxy 监听
2. HTTP 客户端经本地代理实际访问目标（mock 上游转发）
3. 动态同步：rserver 增删代理后仍可用
4. 心跳驱动：失败代理计次 + 退避补测，达到阈值拉黑移出；成功代理保留在队列（不晋级）
5. 无缝切换：上游代理故障后请求自动走可用代理

全部使用本地 mock 上游，不依赖外网。
"""

import asyncio
import logging
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.interfaces.proxy_server import PproxyServer
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework.config import (
    PROXY_GROUPS,
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
    STANDBY_GROUP,
    STANDBY_FILL_GROUPS,
    STANDBY_FILL_PROBE_MULTIPLIER,
    STANDBY_FILL_PROBE_MIN_EXTRA,
    METRICS_WINDOW,
    STANDBY_CHART_WINDOW,
)
from src.use_cases.standby import StandbyQueueUseCase

from tests.e2e.mock_upstream import MOCK_BODY, start_mock_upstream


async def close_server(srv):
    """关闭 asyncio.Server（close 返回 None，非 awaitable）"""
    srv.close()
    await srv.wait_closed()


def free_local_port() -> int:
    """取一个刚释放的本地端口，作为"必然连不上"的目标

    不能用 192.0.2.1:1 这类不可路由地址当"不可达目标"：部分网络（本机即如此）有透明
    代理接管所有 TCP 连接，connect 会立即"成功"，响应内容随环境漂移——曾观测到回 502
    导致用例失败。已释放的本地端口连接必被拒（ConnectionRefusedError），不依赖外网与
    路由，跨环境结果一致。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_pool():
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "default-https", "default-socks5",
                            "freeproxy-world-http", "freeproxy-world-https",
                            "freeproxy-world-socks5", "Proxy-pool-standby-queue", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


def standby_list(pool):
    """代理服务队列组当前代理"""
    return list(pool.proxy_groups[STANDBY_GROUP]["proxies"].keys())


def make_standby(pool, record, check):
    """构造代理服务队列用例（容器壳：pool/record/check_proxies/save）

    e2e 用 mock 上游不走真实网络，network_gate 注入总是 True 的 mock（本地网络恒正常）。
    """
    gate = MagicMock()
    gate.is_ok = True

    async def _gate_ok(timeout):
        return True

    gate.check.side_effect = _gate_ok
    return StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check,
        network_gate=gate,
        standby_group=STANDBY_GROUP,
        fill_groups=STANDBY_FILL_GROUPS,
        fill_probe_multiplier=STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=STANDBY_FILL_PROBE_MIN_EXTRA,
        metrics_window=METRICS_WINDOW,
        chart_window=STANDBY_CHART_WINDOW,
    )


async def http_request_via_proxy(proxy_port: int, host: str = "example.com") -> bytes:
    """以 HTTP 代理模式经本地 pproxy 发起请求，返回响应体

    host 可为 "example.com"（默认，走 mock 上游）或带端口的 "127.0.0.1:<port>"
    （端口由 free_local_port() 取，用于模拟必然连接被拒的目标）。
    """
    if ":" in host and not host.endswith(":443"):
        h, port = host.rsplit(":", 1)
        connect_host = host
    else:
        h, port = host, "80"
        connect_host = f"{host}:{port}"
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"GET http://{connect_host}/ HTTP/1.1\r\nHost: {h}\r\n\r\n".encode())
    await writer.drain()
    resp = await reader.read(4096)
    writer.close()
    await writer.wait_closed()
    return resp


def make_check(success_map):
    """构造 check_proxies mock，按代理返回成功/失败（async 签名）"""
    check = MagicMock()
    check.execute.side_effect = _probe_side_effect({p: success_map.get(p, False) for p in success_map})
    return check


def _probe_side_effect(success_map):
    """探测 side_effect：success_map 内成功，其余失败；调 on_result 模拟流式回调"""
    async def execute(proxies, url, **kw):
        results = _probe_result({p: success_map.get(p, False) for p in proxies})
        on_result = kw.get("on_result")
        if on_result:
            for p in proxies:
                on_result(p, results[p])
        return results
    return execute


def _probe_result(success_map):
    """构造探测结果 dict：success_map 内成功，其余失败"""
    return {
        p: ({"success": True, "status_code": 200, "elapsed": 0.5, "error": None}
            if ok else {"success": False, "status_code": None, "elapsed": 10, "error": "Timeout"})
        for p, ok in success_map.items()
    }


class ServeE2EBase(unittest.TestCase):
    """公共设施：临时目录 + 基础池"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pool = make_pool()
        self.record = RecordProxyUseCase(pool=self.pool, standby_group=STANDBY_GROUP)

    def tearDown(self):
        os.rmdir(self.tmp_dir)


class TestServeE2E(ServeE2EBase):
    """完整链路：补入 -> pproxy 启动 -> HTTP 请求经上游转发"""

    def test_http_request_through_proxy(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-http", proxy_uri)
            self.pool.add_to_group("default-http", "http://127.0.0.1:1")  # 无效代理

            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            resp = await http_request_via_proxy(listen_port)
            self.assertIn(MOCK_BODY, resp)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_traffic_counted(self):
        """经代理转发请求后流量计数器累加（验证 modstat 钩子生效）"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-http", proxy_uri)
            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            await http_request_via_proxy(listen_port)
            await asyncio.sleep(0.3)  # 等转发 task 完成计数

            up, down = server.traffic_total()
            # pproxy modstat 只统计 channel 转发的 body：GET 请求头在 connected 回调里
            # 直接写不经 stat_bytes，故上行可能为 0；响应 body 经 channel 转发必计数
            self.assertGreater(down, 0)
            self.assertGreater(up + down, 0)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_per_proxy_traffic_counted(self):
        """经代理转发后 per_proxy_traffic 按上游代理地址（bind）计数"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-http", proxy_uri)
            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            await http_request_via_proxy(listen_port)
            await asyncio.sleep(0.3)

            bind = f"127.0.0.1:{mock_port}"
            ppt = server.per_proxy_traffic()
            self.assertIn(bind, ppt)
            self.assertGreater(ppt[bind]["download"], 0)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_per_proxy_traffic_history_sampled(self):
        """sample_traffic 后 per_proxy_traffic_history 返回该 bind 的流量采样序列"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-http", proxy_uri)
            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            await http_request_via_proxy(listen_port)
            await asyncio.sleep(0.3)
            server.sample_traffic()

            bind = f"127.0.0.1:{mock_port}"
            hist = server.per_proxy_traffic_history(bind)
            self.assertGreater(len(hist), 0)
            self.assertGreater(hist[-1].total_download, 0)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_per_proxy_latency_history_recorded(self):
        """heartbeat 探测成功后 per_proxy_latency_history 记录该 bind 的延时采样"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-http", proxy_uri)
            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)

            await c.heartbeat("http://probe.local", timeout=3, concurrency=2)

            bind = f"127.0.0.1:{mock_port}"
            hist = c.per_proxy_latency_history(bind)
            self.assertGreater(len(hist), 0)
            self.assertGreaterEqual(hist[-1].latency, 0)

            await close_server(upstream)

        asyncio.run(run())

    def test_dynamic_sync_adds_removes(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"http://127.0.0.1:{mock_port}"

            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start([proxy_uri])
            listen_port = server._handler.sockets[0].getsockname()[1]
            resp = await http_request_via_proxy(listen_port)
            self.assertIn(MOCK_BODY, resp)

            # 动态移除上游 -> 请求目标改为必然被拒的地址（rserver 空时 pproxy 直连）。
            # 断言"不再由 mock 应答"（mock 对任何目标都回 MOCK_BODY）+ "直连失败回明确
            # 错误"（pproxy 连接失败走 error_reply 回 502，而非空响应）。
            dead_port = free_local_port()
            server.sync([])
            resp = await http_request_via_proxy(listen_port, host=f"127.0.0.1:{dead_port}")
            self.assertNotIn(MOCK_BODY, resp)
            self.assertIn(b"502", resp)

            # 动态加回 -> 恢复经 mock 上游转发
            server.sync([proxy_uri])
            resp = await http_request_via_proxy(listen_port)
            self.assertIn(MOCK_BODY, resp)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_https_prefix_converted_to_http_ssl(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"https://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-https", proxy_uri)

            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"https"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            self.assertEqual(standby_list(self.pool), [proxy_uri])

            # 验证转换（单元层面已测，这里确认 serve 可启动）
            from src.interfaces.proxy_server import _pproxy_uri
            self.assertEqual(_pproxy_uri(proxy_uri), f"http+ssl://127.0.0.1:{mock_port}")

            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_socks5_upstream_forwarding(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            proxy_uri = f"socks5://127.0.0.1:{mock_port}"
            self.pool.add_to_group("default-socks5", proxy_uri)

            check = make_check({proxy_uri: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"socks5"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            resp = await http_request_via_proxy(listen_port)
            self.assertIn(MOCK_BODY, resp)

            await server.close()
            await close_server(upstream)

        asyncio.run(run())


class TestHeartbeatE2E(ServeE2EBase):
    """心跳驱动：成功保留在队列、失败计次+退避补测、达到阈值拉黑、无缝切换"""

    def test_heartbeat_success_keeps_failure_removes(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            good = f"http://127.0.0.1:{mock_port}"
            bad = "http://127.0.0.1:1"  # 无效代理

            self.pool.add_to_group("default-http", good)
            self.pool.add_to_group("default-http", bad, consecutive_fails=MAX_CONSECUTIVE_FAILS - 1)  # 探测失败即达阈值拉黑

            check = make_check({good: True, bad: False})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            # 补入验证后入队：bad 探测失败不入队且被拉黑
            self.assertEqual(set(standby_list(self.pool)), {good})
            self.assertTrue(self.pool.is_blacklisted(bad))

            alive = await c.heartbeat("http://probe.local", timeout=3, concurrency=2)

            self.assertEqual(alive, [good])
            self.assertEqual(standby_list(self.pool), [good])
            # 心跳成功不晋级 trusted
            self.assertNotIn(good, self.pool.get_trusted_proxies())

            await close_server(upstream)

        asyncio.run(run())

    def test_heartbeat_failure_retries_then_blacklists(self):
        """心跳失败不立即拉黑：计次进入退避补测，补测失败达阈值才拉黑移出"""
        async def run():
            upstream1, port1 = await start_mock_upstream()
            upstream2, port2 = await start_mock_upstream()
            good1 = f"http://127.0.0.1:{port1}"
            good2 = f"http://127.0.0.1:{port2}"
            bad = "http://127.0.0.1:1"

            self.pool.add_to_group("default-http", good1)
            self.pool.add_to_group("default-http", good2)
            self.pool.add_to_group("default-http", bad, consecutive_fails=MAX_CONSECUTIVE_FAILS - 1)  # 探测失败即达阈值拉黑

            check = make_check({good1: True, good2: True, bad: False})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            self.assertNotIn(bad, standby_list(self.pool))
            self.assertTrue(self.pool.is_blacklisted(bad))

            # good1 心跳失败 -> 计 1 次失败进入退避补测（不立即拉黑，仍在队列）
            check.execute.side_effect = _probe_side_effect({p: p != good1 for p in [good1, good2]})
            alive = await c.heartbeat("http://probe.local", timeout=3, concurrency=2)
            self.assertEqual(alive, [good2])
            self.assertIn(good1, standby_list(self.pool))
            self.assertEqual(c.pending_retries(), [good1])
            self.assertFalse(self.pool.is_blacklisted(good1))

            # 退避补测再失败 -> 达到阈值（MAX_CONSECUTIVE_FAILS=2）拉黑移出
            await c.retry_probe(good1, "http://probe.local", timeout=3)
            self.assertNotIn(good1, standby_list(self.pool))
            self.assertIn(good2, standby_list(self.pool))
            self.assertTrue(self.pool.is_blacklisted(good1))
            self.assertEqual(c.pending_retries(), [])

            await close_server(upstream1)
            await close_server(upstream2)

        asyncio.run(run())

    def test_heartbeat_retry_recovery(self):
        """退避补测成功：清零连续失败计数，代理保留在队列"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            good = f"http://127.0.0.1:{mock_port}"

            self.pool.add_to_group("default-http", good)

            check = make_check({good: True})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
            self.assertEqual(standby_list(self.pool), [good])

            # 第一次心跳失败（瞬时抖动）-> 进入退避补测
            orig_execute = check.execute.side_effect
            check.execute.side_effect = _probe_side_effect({})
            await c.heartbeat("http://probe.local", timeout=3, concurrency=2)
            self.assertEqual(c.pending_retries(), [good])

            # 补测成功 -> 恢复，连续失败计数清零，仍在队列
            check.execute.side_effect = orig_execute
            await c.retry_probe(good, "http://probe.local", timeout=3)
            self.assertEqual(self.pool.get_consecutive_fail_count(good), 0)
            self.assertIn(good, standby_list(self.pool))
            self.assertEqual(c.pending_retries(), [])

            await close_server(upstream)

        asyncio.run(run())

    def test_full_serve_flow_with_heartbeat(self):
        """完整 serve 流：补入 -> start -> 心跳 -> sync -> 请求走存活代理"""
        async def run():
            upstream, mock_port = await start_mock_upstream()
            good = f"http://127.0.0.1:{mock_port}"
            bad = "http://127.0.0.1:1"

            self.pool.add_to_group("default-http", good)
            self.pool.add_to_group("default-http", bad)

            check = make_check({good: True, bad: False})
            c = make_standby(self.pool, self.record, check)
            await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)

            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start(standby_list(self.pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            alive = await c.heartbeat("http://probe.local", timeout=3, concurrency=2)
            server.sync(standby_list(self.pool))

            resp = await http_request_via_proxy(listen_port)
            self.assertIn(MOCK_BODY, resp)
            self.assertEqual(alive, [good])

            await server.close()
            await close_server(upstream)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
