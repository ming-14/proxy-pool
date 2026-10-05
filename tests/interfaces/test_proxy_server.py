"""pproxy 封装（PproxyServer）测试"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.proxy_server import PproxyServer, listen_uri


class _StubTraffic:
    """流量统计用例的最小替身（PproxyServer 只转发 modstat 事件，统计行为在用例层测）"""

    def __init__(self):
        self.pairs = []
        self.bytes_calls = []
        self.conn_calls = []

    def note_pair(self, bind, host):
        self.pairs.append((bind, host))

    def record_bytes(self, bind, host, up, down):
        self.bytes_calls.append((bind, host, up, down))

    def record_conn(self, bind, host, delta):
        self.conn_calls.append((bind, host, delta))


def _realistic_bind(uri: str) -> str:
    """模拟真实 pproxy.Connection 的 bind 属性：只存地址不含 scheme 前缀

    真实 pproxy.Connection("http+ssl://1.2.3.4:443").bind == "1.2.3.4:443"，
    不含 scheme 前缀。sync 须按完整 uri（_uri_index）匹配而非 bind。
    """
    return uri.split("://", 1)[1]


class TestListenUri(unittest.TestCase):
    """pproxy 监听 URI 构造（协议段来自配置的入站协议集合）"""

    def test_multi_protocol(self):
        self.assertEqual(listen_uri("127.0.0.1", 8082, ("http", "socks4", "socks5")),
                         "http+socks4+socks5://127.0.0.1:8082")

    def test_single_protocol(self):
        self.assertEqual(listen_uri("0.0.0.0", 1080, ("socks5",)), "socks5://0.0.0.0:1080")


class TestPproxyServer(unittest.TestCase):
    def setUp(self):
        self.traffic = _StubTraffic()
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080", traffic=self.traffic)

    def tearDown(self):
        self.server._rserver = []
        self.server._uri_index = {}
        self.server._handler = None

    def test_start_builds_rserver(self):
        async def run():
            fake_conn = MagicMock(bind="1.1.1.1:8080")
            with patch("pproxy.Connection", return_value=fake_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock) as mock_start:
                mock_start.return_value = MagicMock()
                await self.server.start(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                self.assertEqual(len(self.server._rserver), 2)
                self.assertEqual(len(self.server._uri_index), 2)
                self.assertIsNotNone(self.server._handler)
        asyncio.run(run())

    def test_start_https_converted(self):
        async def run():
            fake_conn = MagicMock(bind="1.1.1.1:8080")
            with patch("pproxy.Connection", return_value=fake_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["https://1.1.1.1:8080"])
                args = self.server._server.start_server.call_args[0][0]
                self.assertEqual(args["rserver"][0].bind, "1.1.1.1:8080")
                # _uri_index 用完整 uri 作 key（http+ssl://）
                self.assertIn("http+ssl://1.1.1.1:8080", self.server._uri_index)
        asyncio.run(run())

    def test_sync_requires_start(self):
        with self.assertRaises(RuntimeError):
            self.server.sync(["http://1.1.1.1:8080"])

    def test_sync_adds_new(self):
        async def run():
            # mock bind 模拟真实 pproxy（不含前缀），验证 sync 不依赖 bind 而按 uri 匹配
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_realistic_bind(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["http://1.1.1.1:8080"])
                self.server.sync(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                self.assertEqual(set(self.server._uri_index),
                                 {"http://1.1.1.1:8080", "socks5://2.2.2.2:1080"})
                self.assertEqual(len(self.server._rserver), 2)
        asyncio.run(run())

    def test_sync_removes_stale(self):
        async def run():
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_realistic_bind(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                self.server.sync(["http://1.1.1.1:8080"])
                self.assertEqual(set(self.server._uri_index), {"http://1.1.1.1:8080"})
                self.assertEqual(len(self.server._rserver), 1)
        asyncio.run(run())

    def test_sync_idempotent_no_rebuild(self):
        """回归测试：连续 sync 相同列表不应重建 Connection（bind 匹配 bug 会导致全量重建）

        旧 bug：sync 用 Connection.bind（不含前缀）与上游 URI（含前缀）比较，永远
        对不上，每轮全量移除+全量新建。对 http+ssl:// 上游每次创建 2 个 SSLContext
        append 到 pproxy 全局 sslcontexts → 内存泄漏/堆损坏崩溃。
        修复后 sync 按 _uri_index 的完整 uri 匹配，相同列表第二次 sync 零创建。
        """
        async def run():
            created = []
            def track_conn(uri):
                c = MagicMock(bind=_realistic_bind(uri))
                created.append(uri)
                return c
            with patch("pproxy.Connection", side_effect=track_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                proxies = ["https://1.1.1.1:443", "https://2.2.2.2:443", "socks5://3.3.3.3:1080"]
                await self.server.start(proxies)
                self.assertEqual(len(created), 3)  # start 创建 3 个
                # 第二次 sync 相同列表：不应创建任何新 Connection
                self.server.sync(proxies)
                self.assertEqual(len(created), 3, "sync 幂等被破坏：相同列表不应重建 Connection")
                # 第三次 sync 仍然零创建
                self.server.sync(proxies)
                self.assertEqual(len(created), 3)
        asyncio.run(run())

    def test_sync_https_uri_matching(self):
        """https 代理 sync 用 http+ssl:// uri 匹配，不因 bind 不含前缀而误判全量替换"""
        async def run():
            created = []
            def track_conn(uri):
                c = MagicMock(bind=_realistic_bind(uri))
                created.append(uri)
                return c
            with patch("pproxy.Connection", side_effect=track_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["https://1.1.1.1:443", "https://2.2.2.2:443"])
                self.assertEqual(len(created), 2)
                # sync 相同 https 列表：零创建（旧 bug 会全量重建 2 个）
                self.server.sync(["https://1.1.1.1:443", "https://2.2.2.2:443"])
                self.assertEqual(len(created), 2)
                # 换一个代理：只创建 1 个新的，不移除已有的
                self.server.sync(["https://1.1.1.1:443", "https://9.9.9.9:443"])
                self.assertEqual(len(created), 3)
                self.assertEqual(set(self.server._uri_index),
                                 {"http+ssl://1.1.1.1:443", "http+ssl://9.9.9.9:443"})
        asyncio.run(run())

    def test_set_salgorithm_restarts_handler(self):
        async def run():
            handler = MagicMock()
            handler.wait_closed = AsyncMock()
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_realistic_bind(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock) as mock_start:
                mock_start.return_value = handler
                await self.server.start(["http://1.1.1.1:8080"])
                saved_rserver = self.server._rserver
                await self.server.set_salgorithm("fa")
                self.assertEqual(self.server._salgorithm, "fa")
                # 重建 handler（start_server 被再次调用），且上游列表保留
                self.assertEqual(mock_start.call_count, 2)
                args = mock_start.call_args[0][0]
                self.assertEqual(args["salgorithm"], "fa")
                self.assertEqual(args["rserver"], saved_rserver)
        asyncio.run(run())

    def test_set_salgorithm_same_value_skips(self):
        async def run():
            with patch.object(self.server._server, "start_server", new_callable=AsyncMock) as mock_start:
                self.server._salgorithm = "rc"
                await self.server.set_salgorithm("rc")
                # 相同算法：不应触发重建
                mock_start.assert_not_called()
        asyncio.run(run())

    def test_salgorithm_reaches_pproxy(self):
        """回归：调度参数必须以 salgorithm 键名传给 pproxy stream_handler

        旧 bug：start/set_salgorithm 传 s=self._salgorithm，但 vendored
        stream_handler 读取的是 salgorithm（默认 'fa'），s 被 **kwargs 静默吞掉
        → 实际恒为 fa，rc/rr/lc 与 Web 面板调度选择器全部失效。
        本测试用与 ProxySimple.start_server 相同的 partial 绑定方式验证键名。
        """
        import functools
        import inspect

        import pproxy

        listen = self.server._server  # 真实 ProxySimple（URI 解析，无网络）
        args = self.server._handler_args()
        handler = functools.partial(pproxy.server.stream_handler, **vars(listen), **args)
        bound = inspect.signature(pproxy.server.stream_handler).bind_partial(**handler.keywords)
        bound.apply_defaults()
        # salgorithm 必须绑定到实参（否则走默认 'fa'），且 s 不得落入 **kwargs
        self.assertEqual(bound.arguments.get("salgorithm"), "rc")
        self.assertNotIn("s", bound.arguments.get("kwargs", {}))
        # direct_fallback 同理：落入 **kwargs 就被静默忽略，空上游会退回直连
        self.assertIs(bound.arguments.get("direct_fallback"), False)
        self.assertNotIn("direct_fallback", bound.arguments.get("kwargs", {}))

    def test_no_direct_fallback(self):
        """上游为空时不得回退直连（direct_fallback=False 必须传给 pproxy）"""
        async def run():
            handler = MagicMock()
            handler.wait_closed = AsyncMock()
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_realistic_bind(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock) as mock_start:
                mock_start.return_value = handler
                await self.server.start([])
                self.assertIs(mock_start.call_args[0][0]["direct_fallback"], False)
                await self.server.set_salgorithm("lc")
                self.assertIs(mock_start.call_args[0][0]["direct_fallback"], False,
                              "切换调度算法重建 handler 时同样不得开启直连回退")
        asyncio.run(run())


class TestUpstreamHealth(unittest.TestCase):
    """上游存活标记：心跳判定写进 pproxy 上游对象的 alive，让调度过滤器真正生效

    背景：pproxy 的 `alive` 只由 `check_server_alive` 翻转，而它仅在 CLI（`-a`）里启动
    ——本项目不走 CLI，故上游 `alive` 恒为 True、`schedule` 的 filter_cond 形同虚设，
    心跳判失败的代理在补测成功/拉黑前仍会被 rc 随机选中。修复：`sync(unhealthy=...)`
    把代理池的存活判定写进上游对象（存活判定唯一来源 = 代理池心跳）。
    """

    def setUp(self):
        self.traffic = _StubTraffic()
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080", traffic=self.traffic)

    async def _start(self, proxies):
        with patch.object(self.server._server, "start_server", new_callable=AsyncMock):
            await self.server.start(proxies)

    def test_unhealthy_marked_dead(self):
        """unhealthy 中的上游置 alive=False，其余保持 True"""
        async def run():
            a, b = "http://1.1.1.1:8080", "socks5://2.2.2.2:1080"
            await self._start([a, b])
            self.server.sync([a, b], unhealthy=[b])
            self.assertEqual(
                {uri: conn.alive for uri, conn in self.server._uri_index.items()},
                {a: True, b: False},
            )
        asyncio.run(run())

    def test_recovery_restores_alive(self):
        """补测成功（unhealthy 不再包含该代理）后 alive 恢复"""
        async def run():
            a = "http://1.1.1.1:8080"
            await self._start([a])
            self.server.sync([a], unhealthy=[a])
            self.assertFalse(self.server._uri_index[a].alive)
            self.server.sync([a])
            self.assertTrue(self.server._uri_index[a].alive)
        asyncio.run(run())

    def test_flag_survives_membership_change(self):
        """标记随 Connection 复用保持（增删其它上游不影响已标记对象）"""
        async def run():
            a, b = "http://1.1.1.1:8080", "http://2.2.2.2:8080"
            await self._start([a])
            self.server.sync([a], unhealthy=[a])
            conn_a = self.server._uri_index[a]
            self.server.sync([a, b], unhealthy=[a])
            self.assertIs(self.server._uri_index[a], conn_a)
            self.assertFalse(conn_a.alive)
            self.assertTrue(self.server._uri_index[b].alive)
        asyncio.run(run())

    def test_schedule_skips_dead_upstream(self):
        """全部标记为失败时 schedule 返回 None（请求转由 no-direct 拒绝，不再打已知死代理）"""
        async def run():
            import pproxy
            a = "http://1.1.1.1:8080"
            await self._start([a])
            self.server.sync([a], unhealthy=[a])
            self.assertIsNone(pproxy.server.schedule(self.server._rserver, "rc", "example.com", 80))
            self.server.sync([a])
            self.assertIsNotNone(pproxy.server.schedule(self.server._rserver, "rc", "example.com", 80))
        asyncio.run(run())

    def test_no_upstream_state_tracks_all_dead(self):
        """无可用上游状态：列表为空或成员全部失败 → True，任一恢复 → False（驱动告警日志）

        日志本身被测试全局 logging.disable(WARNING) 屏蔽，故断言驱动它的状态位。
        """
        async def run():
            a, b = "http://1.1.1.1:8080", "http://2.2.2.2:8080"
            await self._start([a, b])
            self.assertFalse(self.server._no_upstream)
            self.server.sync([a, b], unhealthy=[a, b])
            self.assertTrue(self.server._no_upstream)
            self.server.sync([a, b], unhealthy=[a])
            self.assertFalse(self.server._no_upstream)
            self.server.sync([], unhealthy=[])
            self.assertTrue(self.server._no_upstream)
        asyncio.run(run())


class TestModstatForwarding(unittest.TestCase):
    """modstat 钩子把字节与连接事件转发到流量统计用例（统计口径在用例层）"""

    def setUp(self):
        self.traffic = _StubTraffic()
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080", traffic=self.traffic)

    def _modstat(self, bind, host):
        return self.server._make_modstat()(None, "127.0.0.1", host, MagicMock(bind=bind))

    def test_forwards_bytes_by_direction(self):
        """i ∈ {0,1} 上行 / {2,3} 下行；host 原样透传（归一化与空值处理在用例层）"""
        m = self._modstat("3.3.3.3:3128", " Example.COM. ")
        m(0)(1000)
        m(2)(3000)
        self.assertEqual(self.traffic.pairs, [("3.3.3.3:3128", " Example.COM. ")])
        self.assertEqual(self.traffic.bytes_calls, [
            ("3.3.3.3:3128", " Example.COM. ", 1000, 0),
            ("3.3.3.3:3128", " Example.COM. ", 0, 3000),
        ])
        self.assertEqual(self.traffic.conn_calls, [])

    def test_forwards_connection_change(self):
        """i ∈ {4,5} 是连接事件（连接级统计的数据源），不再被静默丢弃"""
        m = self._modstat("4.4.4.4:8080", "example.com")
        m(4)(1)
        m(5)(1)
        m(4)(-1)
        self.assertEqual(self.traffic.conn_calls, [
            ("4.4.4.4:8080", "example.com", 1),
            ("4.4.4.4:8080", "example.com", 1),
            ("4.4.4.4:8080", "example.com", -1),
        ])
        self.assertEqual(self.traffic.bytes_calls, [])

    def test_forwards_raw_host_even_when_empty(self):
        """空 host 也照常转发：proxy_server 只做转译，不做业务判断"""
        m = self._modstat("1.1.1.1:8080", "  ")
        m(0)(100)
        self.assertEqual(self.traffic.pairs, [("1.1.1.1:8080", "  ")])
        self.assertEqual(self.traffic.bytes_calls, [("1.1.1.1:8080", "  ", 100, 0)])


if __name__ == "__main__":
    unittest.main()
