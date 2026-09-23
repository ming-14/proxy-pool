"""http_client 的 socks 路由测试

回归点：`AsyncHttpGetter` 原先只对 `socks5://` 走 aiohttp_socks 的 ProxyConnector，
`socks4://` 落到 aiohttp 原生 `proxy=` → aiohttp 直接抛 "Only http and https proxies
are supported" → socks4 代理探测恒失败（正常代理被判心跳失败并拉黑）。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.http_client import AsyncHttpGetter, needs_socks_connector


def _async_cm(value):
    """构造可 `async with` 的 mock（__aenter__ 返回 value）"""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestNeedsSocksConnector(unittest.TestCase):
    def test_socks_schemes(self):
        for proxy in ("socks4://1.2.3.4:1080", "socks5://1.2.3.4:1080"):
            self.assertTrue(needs_socks_connector(proxy), proxy)

    def test_non_socks(self):
        for proxy in ("http://1.2.3.4:8080", "https://1.2.3.4:443", None, ""):
            self.assertFalse(needs_socks_connector(proxy), proxy)


class TestAsyncGetterRouting(unittest.TestCase):
    """socks4/socks5 必须走 ProxyConnector；http/https/直连走共享 session"""

    def _get(self, proxy):
        """跑一次 get()，返回 (status_code, ProxyConnector.from_url 调用参数)"""
        async def run():
            session = MagicMock()
            session.get.return_value = _async_cm(MagicMock(status=204))

            def make_session(*args, **kwargs):
                # socks 分支的临时 session 用 `async with`；共享 session 由 _get_session 直接持有
                return _async_cm(session) if kwargs.get("connector") is not None else session

            with patch("src.interfaces.http_client.aiohttp.ClientSession", make_session), \
                 patch("src.interfaces.http_client.ProxyConnector") as connector:
                status = (await AsyncHttpGetter().get("http://probe.local", timeout=1,
                                                      proxy=proxy)).status_code
                return status, connector.from_url.call_args
        return asyncio.run(run())

    def test_socks4_uses_proxy_connector(self):
        status, called = self._get("socks4://1.2.3.4:1080")
        self.assertEqual(status, 204)
        self.assertEqual(called.args, ("socks4://1.2.3.4:1080",))

    def test_socks5_uses_proxy_connector(self):
        status, called = self._get("socks5://1.2.3.4:1080")
        self.assertEqual(status, 204)
        self.assertEqual(called.args, ("socks5://1.2.3.4:1080",))

    def test_http_proxy_uses_shared_session(self):
        status, called = self._get("http://1.2.3.4:8080")
        self.assertEqual(status, 204)
        self.assertIsNone(called, "http 代理不应走 ProxyConnector")


if __name__ == "__main__":
    unittest.main()