import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from src.interfaces.sources.base import retry_fetch, retry_fetch_async

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)


class TestRetryFetch(unittest.TestCase):
    """共用重试策略：重试次数、异常/空结果重试、accept 谓词（sync/async 同口径）"""

    def test_retries_until_nonempty(self):
        calls = []

        def attempt():
            calls.append(1)
            return [] if len(calls) < 2 else ["a"]

        self.assertEqual(retry_fetch(attempt, retries=3, label="t"), ["a"])
        self.assertEqual(len(calls), 2)

    def test_all_attempts_fail_returns_none(self):
        calls = []

        def attempt():
            calls.append(1)
            raise ValueError("boom")

        self.assertIsNone(retry_fetch(attempt, retries=2, label="t"))
        self.assertEqual(len(calls), 2)

    def test_accept_bypasses_nonempty_rule(self):
        """分页末页空结果：accept 接受空值，不触发无谓重试"""
        calls = []

        def attempt():
            calls.append(1)
            return []

        self.assertEqual(retry_fetch(attempt, retries=3, label="t", accept=lambda _: True), [])
        self.assertEqual(len(calls), 1)

    def test_async_matches_sync_policy(self):
        calls = []

        async def attempt():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("boom")
            return ["x"]

        self.assertEqual(asyncio.run(retry_fetch_async(attempt, retries=3, label="t")), ["x"])

        async def fail():
            raise RuntimeError("boom")

        self.assertIsNone(asyncio.run(retry_fetch_async(fail, retries=2, label="t")))


class TestIP89ProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("src.interfaces.sources.ip89")
        self.Fetcher = self._mod.IP89ProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:8080\n5.6.7.8:3128\n"
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)
        self.assertIn("http://5.6.7.8:3128", proxies)

    def test_fetch_proxies_empty(self):
        with patch("src.interfaces.http_client.get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_no_match(self):
        mock_resp = MagicMock()
        mock_resp.text = "no proxies here"
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_retries(self):
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise Exception("fail")
        with patch("src.interfaces.http_client.get", side_effect=side_effect):
            proxies = self.Fetcher(retries=3).fetch_proxies()
        self.assertEqual(proxies, [])
        self.assertEqual(call_count, 3)

    def test_fetch_proxies_succeeds_on_retry(self):
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("fail")
            mock = MagicMock()
            mock.text = "1.2.3.4:8080\n"
            mock.raise_for_status = MagicMock()
            return mock
        with patch("src.interfaces.http_client.get", side_effect=side_effect):
            proxies = self.Fetcher(retries=3).fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)

    def test_build_url_with_num(self):
        url = self.Fetcher(num=30)._build_url()
        self.assertIn("num=30", url)

    def test_build_url_with_extra_params(self):
        url = self.Fetcher()._build_url(extra_params={"port": "8080"})
        self.assertIn("port=8080", url)

    def test_default_params(self):
        fetcher = self.Fetcher()
        self.assertEqual(fetcher.params["num"], self.Fetcher.DEFAULT_PARAMS["num"])

    def test_custom_timeout(self):
        fetcher = self.Fetcher(timeout=30)
        self.assertEqual(fetcher.timeout, 30)

    def test_custom_base_url(self):
        fetcher = self.Fetcher(base_url="http://custom.url/api")
        self.assertEqual(fetcher.base_url, "http://custom.url/api")

    def test_create_source(self):
        source = self._mod.create_source()
        self.assertTrue(callable(source.fetch_proxies))


class TestGeonodeProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("src.interfaces.sources.geonode")
        self.Fetcher = self._mod.GeonodeProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}],
            "total": 1,
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)

    def test_fetch_proxies_filters_non_http(self):
        mock_resp = MagicMock()
        mock_page1 = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["socks5"]}], "total": 1}
        mock_page2 = {"data": [], "total": 1}
        mock_resp.json.side_effect = [mock_page1, mock_page2]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_pagination(self):
        mock_resp = MagicMock()
        page1 = {
            "data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}],
            "total": 2,
        }
        page2 = {
            "data": [{"ip": "5.6.7.8", "port": 3128, "protocols": ["http"]}],
            "total": 2,
        }
        page3 = {"data": [], "total": 2}
        mock_resp.json.side_effect = [page1, page2, page3]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp) as get:
            proxies = self.Fetcher(params={"limit": "1", "protocols": "http,https"}).fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)
        self.assertIn("http://5.6.7.8:3128", proxies)
        # 3 页各请求一次：末页空页是正常终止，不触发重试（删掉 accept 会让空页重试 3 次）
        self.assertEqual(get.call_count, 3)

    def test_fetch_proxies_empty_data(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [], "total": 0}
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_dedup(self):
        mock_resp = MagicMock()
        page1 = {
            "data": [
                {"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]},
                {"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]},
            ],
            "total": 1,
        }
        page2 = {"data": [], "total": 1}
        mock_resp.json.side_effect = [page1, page2]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(proxies.count("http://1.2.3.4:8080"), 1)

    def test_fetch_proxies_all_retries_fail(self):
        with patch("src.interfaces.http_client.get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=2).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_build_url_with_extra_params(self):
        url = self.Fetcher()._build_url(extra_params={"page": "2"})
        self.assertIn("page=2", url)


class TestGoodipsProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("src.interfaces.sources.goodips")
        self.Fetcher = self._mod.GoodipsProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": [{"ip": "1.2.3.4", "port": 8080}],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)

    def test_fetch_proxies_bad_code(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 1, "data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_empty_data(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_retries(self):
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise Exception("fail")
        with patch("src.interfaces.http_client.get", side_effect=side_effect):
            proxies = self.Fetcher(retries=3).fetch_proxies()
        self.assertEqual(proxies, [])
        self.assertEqual(call_count, 3)

    def test_build_url_with_num(self):
        url = self.Fetcher(num=100)._build_url()
        self.assertIn("num=100", url)

    def test_custom_params(self):
        fetcher = self.Fetcher(params={"num": "20", "protocol": "HTTPS"})
        self.assertEqual(fetcher.params["protocol"], "HTTPS")


class TestProxyScdnFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("src.interfaces.sources.proxy_scdn")
        self.Fetcher = self._mod.ProxyScdnFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["http://1.2.3.4:8080"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)

    def test_fetch_proxies_bad_code(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 500, "data": {"proxies": []}}
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_multiple(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.interfaces.http_client.get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(len(proxies), 2)

    def test_build_url_with_count(self):
        url = self.Fetcher(count=50)._build_url()
        self.assertIn("count=50", url)

    def test_fetch_proxies_retries(self):
        with patch("src.interfaces.http_client.get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=2).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_custom_base_url(self):
        fetcher = self.Fetcher(base_url="http://custom.api/proxy")
        self.assertEqual(fetcher.base_url, "http://custom.api/proxy")


class TestGithubListsFetcher(unittest.TestCase):
    """GitHub 静态列表源：多列表拉取、按列表协议补 scheme、镜像前缀"""

    LIST_HTTP = ("https://raw.example/http.txt", "http")
    LIST_SOCKS = ("https://raw.example/socks5.txt", "socks5")

    def setUp(self):
        import importlib
        self._mod = importlib.import_module("src.interfaces.sources.github_lists")
        self.Fetcher = self._mod.GithubListsFetcher

    @staticmethod
    def _resp(text):
        mock = MagicMock()
        mock.text = text
        mock.raise_for_status = MagicMock()
        return mock

    def test_bare_entries_get_list_protocol(self):
        def fake(url, **kw):
            return self._resp("1.2.3.4:8080\n" if url.endswith("http.txt") else "5.6.7.8:1080\n")
        with patch("src.interfaces.http_client.get", side_effect=fake):
            proxies = self.Fetcher(lists=(self.LIST_HTTP, self.LIST_SOCKS), mirror="").fetch_proxies()
        self.assertIn("http://1.2.3.4:8080", proxies)
        self.assertIn("socks5://5.6.7.8:1080", proxies)

    def test_entry_scheme_wins_over_list_protocol(self):
        """条目自带 scheme 时以条目为准（proxifly 的 https 列表即此形态）"""
        with patch("src.interfaces.http_client.get", return_value=self._resp("https://1.2.3.4:9002\n")):
            proxies = self.Fetcher(lists=(self.LIST_HTTP,), mirror="").fetch_proxies()
        self.assertEqual(proxies, ["https://1.2.3.4:9002"])

    def test_invalid_lines_dropped(self):
        text = "Proxy List\n\n# comment\n1.2.3.4:8080\nnot-a-proxy\n1.2.3.4\ngarbage:port\n"
        with patch("src.interfaces.http_client.get", return_value=self._resp(text)):
            proxies = self.Fetcher(lists=(self.LIST_HTTP,), mirror="").fetch_proxies()
        self.assertEqual(proxies, ["http://1.2.3.4:8080"])

    def test_unsupported_scheme_dropped(self):
        with patch("src.interfaces.http_client.get",
                   return_value=self._resp("socks4://1.2.3.4:1080\n1.2.3.4:8080\n")):
            proxies = self.Fetcher(lists=(self.LIST_HTTP,), mirror="").fetch_proxies()
        self.assertEqual(proxies, ["http://1.2.3.4:8080"])

    def test_one_list_failure_keeps_others(self):
        def fake(url, **kw):
            if url.endswith("http.txt"):
                raise Exception("boom")
            return self._resp("5.6.7.8:1080\n")
        with patch("src.interfaces.http_client.get", side_effect=fake):
            proxies = self.Fetcher(lists=(self.LIST_HTTP, self.LIST_SOCKS), mirror="", retries=1).fetch_proxies()
        self.assertEqual(proxies, ["socks5://5.6.7.8:1080"])

    def test_cross_list_dedup(self):
        lists = (("https://raw.example/a.txt", "http"), ("https://raw.example/b.txt", "http"))
        with patch("src.interfaces.http_client.get", return_value=self._resp("1.2.3.4:8080\n")):
            proxies = self.Fetcher(lists=lists, mirror="").fetch_proxies()
        self.assertEqual(proxies, ["http://1.2.3.4:8080"])

    def test_mirror_prefix_applied(self):
        seen = []

        def fake(url, **kw):
            seen.append(url)
            return self._resp("1.2.3.4:8080\n")
        with patch("src.interfaces.http_client.get", side_effect=fake):
            self.Fetcher(lists=(self.LIST_HTTP,), mirror="https://mirror.example/").fetch_proxies()
        self.assertEqual(seen[0], "https://mirror.example/https://raw.example/http.txt")

    def test_mirror_from_env(self):
        seen = []

        def fake(url, **kw):
            seen.append(url)
            return self._resp("1.2.3.4:8080\n")
        with patch.dict(os.environ, {self._mod.MIRROR_ENV: "http://env-mirror/"}):
            with patch("src.interfaces.http_client.get", side_effect=fake):
                self.Fetcher(lists=(self.LIST_HTTP,)).fetch_proxies()
        self.assertTrue(seen[0].startswith("http://env-mirror/"))

    def test_empty_list_retries_then_succeeds(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return self._resp("") if len(calls) == 1 else self._resp("1.2.3.4:8080\n")
        with patch("src.interfaces.http_client.get", side_effect=fake):
            proxies = self.Fetcher(lists=(self.LIST_HTTP,), mirror="", retries=2).fetch_proxies()
        self.assertEqual(proxies, ["http://1.2.3.4:8080"])
        self.assertEqual(len(calls), 2)

    def test_parse_line(self):
        parse = self._mod.parse_line
        self.assertEqual(parse("1.2.3.4:8080", "http"), "http://1.2.3.4:8080")
        self.assertEqual(parse("socks5://1.2.3.4:1080", "http"), "socks5://1.2.3.4:1080")
        self.assertEqual(parse(" 1.2.3.4:8080 ", "http"), "http://1.2.3.4:8080")
        self.assertEqual(parse("[::1]:1080", "socks5"), "socks5://[::1]:1080")
        self.assertIsNone(parse("", "http"))
        self.assertIsNone(parse("# comment", "http"))
        self.assertIsNone(parse("1.2.3.4", "http"))

    def test_default_lists_cover_all_protocols(self):
        """默认列表须覆盖三种协议，否则对应分组永远为空"""
        self.assertEqual({p for _, p in self._mod.LISTS}, {"http", "https", "socks5"})

    def test_create_source(self):
        source = self._mod.create_source()
        self.assertTrue(callable(source.fetch_proxies))


if __name__ == "__main__":
    unittest.main()
