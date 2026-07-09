import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)


class TestIP89ProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("proxy_pool.89ip-cn")
        self.Fetcher = self._mod.IP89ProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:8080\n5.6.7.8:3128\n"
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)
        self.assertIn("5.6.7.8:3128", proxies)

    def test_fetch_proxies_empty(self):
        with patch.object(self._mod.requests, "get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_no_match(self):
        mock_resp = MagicMock()
        mock_resp.text = "no proxies here"
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_retries(self):
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise Exception("fail")
        with patch.object(self._mod.requests, "get", side_effect=side_effect):
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
        with patch.object(self._mod.requests, "get", side_effect=side_effect):
            proxies = self.Fetcher(retries=3).fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)

    def test_build_url_with_num(self):
        url = self.Fetcher(num=30)._build_url()
        self.assertIn("num=30", url)

    def test_build_url_with_extra_params(self):
        url = self.Fetcher()._build_url(extra_params={"port": "8080"})
        self.assertIn("port=8080", url)

    def test_default_params(self):
        fetcher = self.Fetcher()
        self.assertEqual(fetcher.params["num"], self._mod.DEFAULT_PARAMS["num"])

    def test_custom_timeout(self):
        fetcher = self.Fetcher(timeout=30)
        self.assertEqual(fetcher.timeout, 30)

    def test_custom_base_url(self):
        fetcher = self.Fetcher(base_url="http://custom.url/api")
        self.assertEqual(fetcher.base_url, "http://custom.url/api")

    def test_module_level_fetch_proxies(self):
        self.assertTrue(callable(self._mod.fetch_proxies))


class TestGeonodeProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("proxy_pool.geonode-com")
        self.Fetcher = self._mod.GeonodeProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}],
            "total": 1,
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)

    def test_fetch_proxies_filters_non_http(self):
        mock_resp = MagicMock()
        mock_page1 = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["socks5"]}], "total": 1}
        mock_page2 = {"data": [], "total": 1}
        mock_resp.json.side_effect = [mock_page1, mock_page2]
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
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
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher(params={"limit": "1", "protocols": "http,https"}).fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)
        self.assertIn("5.6.7.8:3128", proxies)

    def test_fetch_proxies_empty_data(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [], "total": 0}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
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
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(proxies.count("1.2.3.4:8080"), 1)

    def test_fetch_proxies_all_retries_fail(self):
        with patch.object(self._mod.requests, "get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=2).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_build_url_with_extra_params(self):
        url = self.Fetcher()._build_url(extra_params={"page": "2"})
        self.assertIn("page=2", url)


class TestGoodipsProxyFetcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self._mod = importlib.import_module("proxy_pool.goodips-com")
        self.Fetcher = self._mod.GoodipsProxyFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": [{"ip": "1.2.3.4", "port": 8080}],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)

    def test_fetch_proxies_bad_code(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 1, "data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_empty_data(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_retries(self):
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise Exception("fail")
        with patch.object(self._mod.requests, "get", side_effect=side_effect):
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
        self._mod = importlib.import_module("proxy_pool.proxy-scdn-io")
        self.Fetcher = self._mod.ProxyScdnFetcher

    def test_fetch_proxies_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["1.2.3.4:8080"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertIn("1.2.3.4:8080", proxies)

    def test_fetch_proxies_bad_code(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 500, "data": {"proxies": []}}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher(retries=1).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_fetch_proxies_multiple(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["1.2.3.4:8080", "5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(self._mod.requests, "get", return_value=mock_resp):
            proxies = self.Fetcher().fetch_proxies()
        self.assertEqual(len(proxies), 2)

    def test_build_url_with_count(self):
        url = self.Fetcher(count=50)._build_url()
        self.assertIn("count=50", url)

    def test_fetch_proxies_retries(self):
        with patch.object(self._mod.requests, "get", side_effect=Exception("fail")):
            proxies = self.Fetcher(retries=2).fetch_proxies()
        self.assertEqual(proxies, [])

    def test_custom_base_url(self):
        fetcher = self.Fetcher(base_url="http://custom.api/proxy")
        self.assertEqual(fetcher.base_url, "http://custom.api/proxy")


if __name__ == "__main__":
    unittest.main()
