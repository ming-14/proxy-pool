"""http_client 的 socks 路由测试

回归点：`AsyncHttpGetter` 原先只对 `socks5://` 走 aiohttp_socks 的 ProxyConnector，
`socks4://` 落到 aiohttp 原生 `proxy=` → aiohttp 直接抛 "Only http and https proxies
are supported" → socks4 代理探测恒失败（正常代理被判心跳失败并拉黑）。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.interfaces.http_client import AsyncHttpGetter
from src.interfaces.proxy_scheme import needs_socks_connector


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

    def test_socks_connector_requests_remote_dns(self):
        """必须显式传 rdns=True：python_socks 的 SOCKS4 连接器把 rdns=None 当 False，
        会在本机解析目标域名后只发 IPv4（与"域名交上游解析"的不变量矛盾，且本地
        DNS 抖动/污染会让 socks4 探测误判失败）"""
        for proxy in ("socks4://1.2.3.4:1080", "socks5://1.2.3.4:1080"):
            with self.subTest(proxy=proxy):
                _, called = self._get(proxy)
                self.assertIs(called.kwargs.get("rdns"), True)

    def test_http_proxy_uses_shared_session(self):
        status, called = self._get("http://1.2.3.4:8080")
        self.assertEqual(status, 204)
        self.assertIsNone(called, "http 代理不应走 ProxyConnector")


class TestAsyncGetJson(unittest.TestCase):
    """get_json：复用 get 的代理链路，非 200 抛 RuntimeError、坏 JSON 抛 ValueError

    供 DohResolver 按端点回退（见 tests/interfaces/test_doh_resolver.py）。
    """

    def _get_json(self, proxy, status, body):
        """跑一次 get_json，返回 (解析值 | None, 异常 | None, ProxyConnector 调用参数)"""
        async def run():
            resp = MagicMock(status=status)
            resp.text = AsyncMock(return_value=body)
            session = MagicMock()
            session.get.return_value = _async_cm(resp)

            def make_session(*args, **kwargs):
                return _async_cm(session) if kwargs.get("connector") is not None else session

            with patch("src.interfaces.http_client.aiohttp.ClientSession", make_session), \
                 patch("src.interfaces.http_client.ProxyConnector") as connector:
                try:
                    value = await AsyncHttpGetter().get_json(
                        "https://doh.local/resolve?name=example.com&type=A",
                        timeout=3, headers={"Accept": "application/dns-json"},
                        proxy=proxy, verify=True)
                    return value, None, connector.from_url.call_args
                except Exception as exc:
                    return None, exc, connector.from_url.call_args
        return asyncio.run(run())

    def test_parses_json_via_shared_session(self):
        value, exc, called = self._get_json(
            "http://1.2.3.4:8080", 200, '{"Answer": [{"type": 1, "data": "1.2.3.4"}]}')
        self.assertIsNone(exc)
        self.assertEqual(value, {"Answer": [{"type": 1, "data": "1.2.3.4"}]})
        self.assertIsNone(called, "http 代理不应走 ProxyConnector")

    def test_socks_proxy_uses_connector_with_remote_dns(self):
        value, exc, called = self._get_json("socks5://1.2.3.4:1080", 200, '{"Status": 0}')
        self.assertIsNone(exc)
        self.assertEqual(value, {"Status": 0})
        self.assertEqual(called.args, ("socks5://1.2.3.4:1080",))
        self.assertIs(called.kwargs.get("rdns"), True)

    def test_non_200_raises_runtime_error(self):
        _, exc, _ = self._get_json("http://1.2.3.4:8080", 503, "upstream down")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("503", str(exc))

    def test_bad_json_raises_value_error(self):
        _, exc, _ = self._get_json("http://1.2.3.4:8080", 200, "<html>not json</html>")
        self.assertIsInstance(exc, ValueError)


if __name__ == "__main__":
    unittest.main()