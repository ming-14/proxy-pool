"""pproxy 封装（PproxyServer）测试"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.proxy_server import PproxyServer, _pproxy_uri


def _realistic_bind(uri: str) -> str:
    """模拟真实 pproxy.Connection 的 bind 属性：只存地址不含 scheme 前缀

    真实 pproxy.Connection("http+ssl://1.2.3.4:443").bind == "1.2.3.4:443"，
    不含 scheme 前缀。sync 须按完整 uri（_uri_index）匹配而非 bind。
    """
    return uri.split("://", 1)[1]


class TestPproxyUri(unittest.TestCase):
    def test_http_kept(self):
        self.assertEqual(_pproxy_uri("http://1.1.1.1:8080"), "http://1.1.1.1:8080")

    def test_socks5_kept(self):
        self.assertEqual(_pproxy_uri("socks5://2.2.2.2:1080"), "socks5://2.2.2.2:1080")

    def test_https_converted_to_http_ssl(self):
        # https 代理在 pproxy 中用 http+ssl://（TLS 到代理）
        self.assertEqual(_pproxy_uri("https://3.3.3.3:443"), "http+ssl://3.3.3.3:443")


class TestPproxyServer(unittest.TestCase):
    def setUp(self):
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080")

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

        旧 bug：sync 用 Connection.bind（不含前缀）与 _pproxy_uri（含前缀）比较，永远
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
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080")

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


class TestPerProxyTraffic(unittest.TestCase):
    """按上游代理 IP 的流量统计测试"""

    def setUp(self):
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080")

    def test_empty_initially(self):
        """初始无流量时 per_proxy_traffic 返回空 dict"""
        self.assertEqual(self.server.per_proxy_traffic(), {})

    def test_add_upload_download_per_bind(self):
        """_add_upload/_add_download 按 bind 累计，全局与 per-proxy 一致"""
        self.server._add_upload("1.1.1.1:8080", 100)
        self.server._add_download("1.1.1.1:8080", 200)
        self.server._add_upload("2.2.2.2:1080", 50)
        self.server._add_download("2.2.2.2:1080", 150)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["1.1.1.1:8080"], {"upload": 100, "download": 200})
        self.assertEqual(traffic["2.2.2.2:1080"], {"upload": 50, "download": 150})
        # 全局总计
        total_up, total_down = self.server.traffic_total()
        self.assertEqual(total_up, 150)
        self.assertEqual(total_down, 350)

    def test_direct_bind(self):
        """显式 direct:// 上游（bind == "DIRECT"）的流量仍用 'DIRECT' 作 key"""
        self.server._add_upload("DIRECT", 500)
        self.server._add_download("DIRECT", 800)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["DIRECT"], {"upload": 500, "download": 800})

    def test_modstat_records_per_proxy(self):
        """modstat 钩子按 roption.bind 分发计数（roption 由 pproxy stream_handler 传入）"""
        roption = MagicMock(bind="3.3.3.3:3128")
        modstat = self.server._make_modstat()
        m = modstat(None, "127.0.0.1", "example.com", roption)
        m(0)(1000)
        m(2)(3000)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["3.3.3.3:3128"], {"upload": 1000, "download": 3000})

    def test_modstat_connection_change_ignored(self):
        """i ∈ {4,5}（连接数变化）不统计流量"""
        roption = MagicMock(bind="4.4.4.4:8080")
        modstat = self.server._make_modstat()
        m = modstat(None, "127.0.0.1", "example.com", roption)
        m(4)(1)
        m(5)(1)
        self.assertEqual(self.server.per_proxy_traffic(), {})


class TestPerProxyTrafficHistory(unittest.TestCase):
    """按上游代理 IP 的流量历史采样测试"""

    def setUp(self):
        self.server = PproxyServer("http+socks4+socks5://127.0.0.1:8080")

    def test_empty_initially(self):
        self.assertEqual(self.server.per_proxy_traffic_history("1.1.1.1:8080"), [])

    def test_sample_records_per_bind(self):
        """sample_traffic 为每个有流量的 bind 各采一笔"""
        self.server._add_upload("1.1.1.1:8080", 100)
        self.server._add_download("1.1.1.1:8080", 200)
        self.server._add_upload("2.2.2.2:1080", 50)
        self.server.sample_traffic()
        h1 = self.server.per_proxy_traffic_history("1.1.1.1:8080")
        h2 = self.server.per_proxy_traffic_history("2.2.2.2:1080")
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0].total_upload, 100)
        self.assertEqual(h1[0].total_download, 200)
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0].total_upload, 50)

    def test_multiple_samples_accumulate(self):
        """多次采样各记一笔，累计值递增"""
        self.server._add_download("1.1.1.1:8080", 100)
        self.server.sample_traffic()
        self.server._add_download("1.1.1.1:8080", 200)
        self.server.sample_traffic()
        h = self.server.per_proxy_traffic_history("1.1.1.1:8080")
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0].total_download, 100)
        self.assertEqual(h[1].total_download, 300)

    def test_window_cleanup(self):
        """超出 metrics_window 的采样点自动清理"""
        server = PproxyServer("http://127.0.0.1:8080", metrics_window=0.1)
        server._add_upload("1.1.1.1:8080", 100)
        server.sample_traffic()
        import time as _time
        _time.sleep(0.15)
        server._add_upload("1.1.1.1:8080", 200)
        server.sample_traffic()
        h = server.per_proxy_traffic_history("1.1.1.1:8080")
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0].total_upload, 300)

    def test_unknown_bind_returns_empty(self):
        """未产生流量的 bind 返回空列表"""
        self.server._add_upload("1.1.1.1:8080", 100)
        self.server.sample_traffic()
        self.assertEqual(self.server.per_proxy_traffic_history("9.9.9.9:9999"), [])


if __name__ == "__main__":
    unittest.main()
