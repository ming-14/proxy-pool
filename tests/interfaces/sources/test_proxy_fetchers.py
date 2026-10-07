import asyncio
import importlib
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from src.domain.models import SUPPORTED_SCHEMES
from src.interfaces.sources.base import parse_line, retry_fetch, retry_fetch_async

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


class TestParseLine(unittest.TestCase):
    """纯文本单行规范化（base.parse_line，github_lists / proxyscrape 共用）"""

    def test_normalizes_supported_entries(self):
        self.assertEqual(parse_line("1.2.3.4:8080", "http"), "http://1.2.3.4:8080")
        self.assertEqual(parse_line("socks5://1.2.3.4:1080", "http"), "socks5://1.2.3.4:1080")
        self.assertEqual(parse_line(" 1.2.3.4:8080 ", "http"), "http://1.2.3.4:8080")
        self.assertEqual(parse_line("[::1]:1080", "socks5"), "socks5://[::1]:1080")

    def test_invalid_lines_dropped(self):
        self.assertIsNone(parse_line("", "http"))
        self.assertIsNone(parse_line("# comment", "http"))
        self.assertIsNone(parse_line("1.2.3.4", "http"))
        self.assertIsNone(parse_line("socks4://1.2.3.4:1080", "http"))  # 不受支持协议

    def test_inline_comment_stripped(self):
        """行内注释同整行注释口径剥离（`socks5://ip:port #hk` 靠这个才不为 0 条）"""
        self.assertEqual(parse_line("socks5://1.2.3.4:1080 #hk", "http"), "socks5://1.2.3.4:1080")
        self.assertEqual(parse_line("1.2.3.4:8080 # HTTP [TH]", "http"), "http://1.2.3.4:8080")


class TestGithubListsFetcher(unittest.TestCase):
    """GitHub 静态列表源：多列表拉取、按列表协议补 scheme、镜像前缀"""

    LIST_HTTP = ("https://raw.example/http.txt", "http")
    LIST_SOCKS = ("https://raw.example/socks5.txt", "socks5")

    def setUp(self):
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

    def test_default_lists_cover_all_protocols(self):
        """默认列表须覆盖三种协议，否则对应分组永远为空"""
        self.assertEqual({p for _, p in self._mod.LISTS}, {"http", "https", "socks5"})

    def test_default_lists_well_formed(self):
        """列表准入约束：协议受支持、无 socks4、路径唯一（重复会白拉一遍）"""
        for path, proto in self._mod.LISTS:
            self.assertIn(proto, SUPPORTED_SCHEMES, path)
        rels = [path[len(self._mod._RAW_ROOT):] for path, _ in self._mod.LISTS]
        self.assertEqual(len(rels), len(set(rels)), "存在重复列表路径")

    def test_create_source(self):
        source = self._mod.create_source()
        self.assertTrue(callable(source.fetch_proxies))


class TestFreeApiSources(unittest.TestCase):
    """四个免费代理 API 源的 _parse：协议归类、socks4 丢弃、畸形条目丢弃"""

    @staticmethod
    def _json_resp(payload):
        mock = MagicMock()
        mock.json.return_value = payload
        mock.raise_for_status = MagicMock()
        return mock

    @staticmethod
    def _text_resp(text):
        mock = MagicMock()
        mock.text = text
        mock.raise_for_status = MagicMock()
        return mock

    def test_hproxy_multi_protocol_picks_one(self):
        """protocols 混入 socks4/多协议时只取一个受支持 scheme，纯 socks4 与缺字段丢弃"""
        from src.interfaces.sources.hproxy import HproxyFetcher
        data = [
            {"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]},
            {"ip": "5.6.7.8", "port": 1080, "protocols": ["socks4", "socks5"]},
            {"ip": "9.9.9.9", "port": 1080, "protocols": ["socks4"]},
            {"ip": "8.8.8.8", "port": 80, "protocols": ["HTTP", "HTTPS"]},
            {"ip": "7.7.7.7", "port": 0, "protocols": ["http"]},
            {"port": 8080, "protocols": ["http"]},
        ]
        self.assertEqual(HproxyFetcher()._parse(self._json_resp(data)), [
            "http://1.2.3.4:8080",
            "socks5://5.6.7.8:1080",
            "http://8.8.8.8:80",
        ])

    def test_proxyscrape_keeps_schemed_lines_only(self):
        """自带 scheme 的行按条目协议归组；socks4 与畸形行丢弃"""
        from src.interfaces.sources.proxyscrape import ProxyScrapeFetcher
        text = ("http://1.2.3.4:8080\nsocks5://5.6.7.8:1080\nsocks4://9.9.9.9:1080\n"
                "not-a-proxy\n1.2.3.4\n\n")
        self.assertEqual(ProxyScrapeFetcher()._parse(self._text_resp(text)), [
            "http://1.2.3.4:8080",
            "socks5://5.6.7.8:1080",
        ])

    def test_proxylister_uses_ip_address_field(self):
        """proxylister 字段名是 ip_address，且 protocols 大写、常含 SOCKS4"""
        from src.interfaces.sources.proxylister import ProxylisterFetcher
        data = {"results": [
            {"ip_address": "1.2.3.4", "port": 8080, "protocols": ["HTTP"]},
            {"ip_address": "5.6.7.8", "port": 1080, "protocols": ["SOCKS4", "HTTP", "SOCKS5"]},
            {"ip_address": "9.9.9.9", "port": 1080, "protocols": ["SOCKS4"]},
            {"ip_address": "8.8.8.8", "port": None, "protocols": ["HTTP"]},
        ], "next": None}
        self.assertEqual(ProxylisterFetcher()._parse(self._json_resp(data)), [
            "http://1.2.3.4:8080",
            "http://5.6.7.8:1080",
        ])

    def test_imtaqin_single_uppercase_protocol(self):
        """imtaqin 单协议条目且协议名大写；SOCKS4 丢弃"""
        from src.interfaces.sources.imtaqin import ImtaqinFetcher
        data = {"count": 4, "proxies": [
            {"ip": "1.2.3.4", "port": 8080, "protocol": "HTTP"},
            {"ip": "5.6.7.8", "port": 1080, "protocol": "SOCKS5"},
            {"ip": "9.9.9.9", "port": 4145, "protocol": "SOCKS4"},
            {"ip": "8.8.8.8", "port": 443, "protocol": "HTTPS"},
        ]}
        self.assertEqual(ImtaqinFetcher()._parse(self._json_resp(data)), [
            "http://1.2.3.4:8080",
            "socks5://5.6.7.8:1080",
            "https://8.8.8.8:443",
        ])

    def test_create_source_entrypoints(self):
        """加载器入口可调用且符合 ProxySource 契约（container._source_loader 依赖它）"""
        for mod in ("hproxy", "proxyscrape", "proxylister", "imtaqin"):
            with self.subTest(mod=mod):
                source = importlib.import_module(f"src.interfaces.sources.{mod}").create_source(config=None)
                self.assertTrue(callable(source.fetch_proxies))


if __name__ == "__main__":
    unittest.main()
