"""pproxy 封装（PproxyServer）测试"""
import asyncio
import contextvars
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.proxy_server import PproxyServer, _pproxy_uri, _current_roption


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
                self.assertEqual(args["s"], "fa")
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
        """DIRECT（直连）流量用 'DIRECT' 作 key"""
        self.server._add_upload("DIRECT", 500)
        self.server._add_download("DIRECT", 800)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["DIRECT"], {"upload": 500, "download": 800})

    def test_modstat_records_per_proxy(self):
        """modstat 钩子在 contextvars 设置 roption 后按 bind 分发计数"""
        roption = MagicMock(bind="3.3.3.3:3128")
        ctx = contextvars.copy_context()
        ctx.run(_current_roption.set, roption)
        modstat = ctx.run(self.server._make_modstat)
        m = ctx.run(modstat, None, "127.0.0.1", "example.com")
        # i=0 上行, i=2 下行
        ctx.run(m(0), 1000)
        ctx.run(m(2), 3000)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["3.3.3.3:3128"], {"upload": 1000, "download": 3000})

    def test_modstat_direct_when_no_roption(self):
        """contextvars 无 roption（schedule 返回 None）时归入 DIRECT"""
        ctx = contextvars.copy_context()
        ctx.run(_current_roption.set, None)
        modstat = ctx.run(self.server._make_modstat)
        m = ctx.run(modstat, None, "127.0.0.1", "example.com")
        ctx.run(m(0), 200)
        traffic = self.server.per_proxy_traffic()
        self.assertEqual(traffic["DIRECT"], {"upload": 200, "download": 0})

    def test_modstat_connection_change_ignored(self):
        """i ∈ {4,5}（连接数变化）不统计流量"""
        roption = MagicMock(bind="4.4.4.4:8080")
        ctx = contextvars.copy_context()
        ctx.run(_current_roption.set, roption)
        modstat = ctx.run(self.server._make_modstat)
        m = ctx.run(modstat, None, "127.0.0.1", "example.com")
        ctx.run(m(4), 1)
        ctx.run(m(5), 1)
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
