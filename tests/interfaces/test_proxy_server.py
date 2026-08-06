"""pproxy 封装（PproxyServer）测试"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.proxy_server import PproxyServer, _pproxy_uri


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
        self.server._handler = None

    def test_start_builds_rserver(self):
        async def run():
            fake_conn = MagicMock(bind="1.1.1.1:8080")
            with patch("pproxy.Connection", return_value=fake_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock) as mock_start:
                mock_start.return_value = MagicMock()
                await self.server.start(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                self.assertEqual(len(self.server._rserver), 2)
                self.assertIsNotNone(self.server._handler)
        asyncio.run(run())

    def test_start_https_converted(self):
        async def run():
            fake_conn = MagicMock(bind="http://1.1.1.1:8080")
            with patch("pproxy.Connection", return_value=fake_conn), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["https://1.1.1.1:8080"])
                args = self.server._server.start_server.call_args[0][0]
                self.assertEqual(args["rserver"][0].bind, "http://1.1.1.1:8080")
        asyncio.run(run())

    def test_sync_requires_start(self):
        with self.assertRaises(RuntimeError):
            self.server.sync(["http://1.1.1.1:8080"])

    def test_sync_adds_new(self):
        async def run():
            fake_conn = MagicMock(bind="http://1.1.1.1:8080")
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_pproxy_uri(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["http://1.1.1.1:8080"])
                # 新增代理
                self.server.sync(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                binds = {getattr(o, "bind", None) for o in self.server._rserver}
                self.assertEqual(binds, {"http://1.1.1.1:8080", "socks5://2.2.2.2:1080"})
        asyncio.run(run())

    def test_sync_removes_stale(self):
        async def run():
            with patch("pproxy.Connection", side_effect=lambda uri: MagicMock(bind=_pproxy_uri(uri))), \
                 patch.object(self.server._server, "start_server", new_callable=AsyncMock):
                await self.server.start(["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"])
                # 移除 socks5
                self.server.sync(["http://1.1.1.1:8080"])
                binds = {getattr(o, "bind", None) for o in self.server._rserver}
                self.assertEqual(binds, {"http://1.1.1.1:8080"})
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
