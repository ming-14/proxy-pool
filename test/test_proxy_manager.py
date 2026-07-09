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

from manager import ProxyManager
from config import MAX_FAILS, TRUSTED_MAX_FAILS, SOURCE_PAUSE_TTL, PROXY_GROUPS, SOURCES as _SOURCES

SOURCES = [{**s, "enabled": True} for s in _SOURCES]


class TestProxyManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")
        self._patcher = patch("manager.SOURCES", SOURCES)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_empty_cache(self):
        pm = ProxyManager(cache_file=self.cache_file)
        for name in PROXY_GROUPS:
            self.assertEqual(len(pm._cache.proxy_groups[name]["proxies"]), 0)

    def test_record_success_moves_to_trusted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm.record_success("1.2.3.4:8080")
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["default"]["proxies"])
        self.assertIn("1.2.3.4:8080", pm._cache.proxy_groups["trusted"]["proxies"])

    def test_record_success_already_trusted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("trusted", "1.2.3.4:8080", fails=1)
        pm.record_success("1.2.3.4:8080")
        self.assertIn("1.2.3.4:8080", pm._cache.proxy_groups["trusted"]["proxies"])
        self.assertEqual(pm._cache.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"]["fails"], 1)

    def test_record_success_resets_fails(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080", fails=1)
        pm.record_success("1.2.3.4:8080")
        self.assertEqual(pm._cache.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"]["fails"], 0)

    def test_record_fail(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm.record_fail("1.2.3.4:8080")
        self.assertEqual(pm._cache.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["fails"], 1)

    def test_record_fail_trusted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("trusted", "1.2.3.4:8080")
        pm.record_fail("1.2.3.4:8080")
        self.assertEqual(pm._cache.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"]["fails"], 1)

    def test_record_fail_nonexistent(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm.record_fail("0.0.0.0:0")

    def test_remove_moves_to_blacklist(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm.remove("1.2.3.4:8080")
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["default"]["proxies"])
        self.assertTrue(pm._cache.is_blacklisted("1.2.3.4:8080"))

    def test_remove_trusted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("trusted", "1.2.3.4:8080")
        pm.remove("1.2.3.4:8080")
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["trusted"]["proxies"])
        self.assertTrue(pm._cache.is_blacklisted("1.2.3.4:8080"))

    def test_remove_nonexistent(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm.remove("0.0.0.0:0")
        self.assertTrue(pm._cache.is_blacklisted("0.0.0.0:0"))

    def test_get_proxies_returns_available(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm._cache.add_to_group("default", "5.6.7.8:3128", fails=MAX_FAILS)
        proxies = pm.get_proxies(1)
        self.assertEqual(proxies, ["1.2.3.4:8080"])

    def test_get_proxies_none_when_all_exhausted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080", fails=MAX_FAILS)
        with patch.object(pm, "_fetch_proxies"):
            proxies = pm.get_proxies(1)
        self.assertEqual(proxies, [])

    def test_get_proxies_with_exclude(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm._cache.add_to_group("default", "5.6.7.8:3128")
        proxies = pm.get_proxies(1, exclude={"1.2.3.4:8080"})
        self.assertEqual(proxies, ["5.6.7.8:3128"])

    def test_get_proxies_auto_fetch_on_empty(self):
        pm = ProxyManager(cache_file=self.cache_file)
        with patch.object(pm, "_fetch_proxies") as mock_fetch:
            pm.get_proxies(1)
        mock_fetch.assert_called_once()

    def test_get_proxies_no_auto_fetch_when_available(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        with patch.object(pm, "_fetch_proxies") as mock_fetch:
            pm.get_proxies(1)
        mock_fetch.assert_not_called()

    def test_get_proxies_returns_at_most_n(self):
        pm = ProxyManager(cache_file=self.cache_file)
        for i in range(5):
            pm._cache.add_to_group("default", f"{i}.0.0.0:8080")
        proxies = pm.get_proxies(3)
        self.assertEqual(len(proxies), 3)

    def test_get_proxies_returns_all_if_n_exceeds(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm._cache.add_to_group("default", "5.6.7.8:3128")
        proxies = pm.get_proxies(10)
        self.assertEqual(len(proxies), 2)

    def test_auto_remove_on_max_fails(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080", fails=MAX_FAILS - 1)
        pm.record_fail("1.2.3.4:8080")
        pm.remove("1.2.3.4:8080")
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["default"]["proxies"])

    def test_trusted_removed_on_max_fails(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("trusted", "1.2.3.4:8080", fails=TRUSTED_MAX_FAILS - 1)
        pm.record_fail("1.2.3.4:8080")
        pm.remove("1.2.3.4:8080")
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["trusted"]["proxies"])

    def test_fetch_proxies_adds_to_correct_group(self):
        pm = ProxyManager(cache_file=self.cache_file)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertIn("5.6.7.8:3128", pm._cache.proxy_groups["default"]["proxies"])

    def test_fetch_proxies_no_duplicate(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080", fails=1)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["1.2.3.4:8080", "5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertEqual(pm._cache.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["fails"], 1)

    def test_fetch_proxies_skips_blacklisted(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("blacklist", "1.2.3.4:8080")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["1.2.3.4:8080", "5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertNotIn("1.2.3.4:8080", pm._cache.proxy_groups["default"]["proxies"])

    def test_freeproxy_world_paused_after_fetch(self):
        pm = ProxyManager(cache_file=self.cache_file)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertTrue(pm._cache.is_source_paused("proxy_pool.freeproxy-world"))
        self.assertEqual(pm._cache.source_paused["proxy_pool.freeproxy-world"]["ttl"], SOURCES[4]["pause_ttl"])

    def test_freeproxy_world_paused_even_on_failure(self):
        pm = ProxyManager(cache_file=self.cache_file)
        with patch("manager.requests.get", side_effect=Exception("fail")):
            pm._fetch_proxies()
        self.assertTrue(pm._cache.is_source_paused("proxy_pool.freeproxy-world"))

    def test_freeproxy_world_proxies_in_own_group(self):
        pm = ProxyManager(cache_file=self.cache_file)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": ["5.6.7.8:3128"]},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_mod = MagicMock()
        mock_mod.fetch_proxies.return_value = ["1.2.3.4:8080"]
        with patch("manager.requests.get", return_value=mock_resp):
            with patch("importlib.import_module", side_effect=lambda src: mock_mod if src == "proxy_pool.freeproxy-world" else __import__(src)):
                pm._fetch_proxies()
        self.assertIn("1.2.3.4:8080", pm._cache.proxy_groups["freeproxy-world"]["proxies"])

    def test_source_paused_on_high_dedup(self):
        pm = ProxyManager(cache_file=self.cache_file)
        for i in range(95):
            pm._cache.add_to_group("trusted", f"{i}.0.0.0:80")
        pm._cache.add_to_group("blacklist", "95.0.0.0:80")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": [f"{i}.0.0.0:80" for i in range(100)]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertIn("proxy_pool.proxy-scdn-io", pm._cache.source_paused)

    def test_source_not_paused_below_threshold(self):
        pm = ProxyManager(cache_file=self.cache_file)
        for i in range(50):
            pm._cache.add_to_group("trusted", f"{i}.0.0.0:80")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"proxies": [f"{i}.0.0.0:80" for i in range(100)]},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        dedup_paused = [s for s in pm._cache.source_paused if pm._cache.source_paused[s].get("ttl") != SOURCES[4]["pause_ttl"]]
        self.assertEqual(len(dedup_paused), 0)

    def test_trusted_survives_default_expiry(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm._cache.add_to_group("trusted", "9.9.9.9:80")
        pm._cache.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["joined_at"] = time.time() - PROXY_GROUPS["default"] - 1
        pm.save_cache()
        pm2 = ProxyManager(cache_file=self.cache_file)
        self.assertNotIn("1.2.3.4:8080", pm2._cache.proxy_groups["default"]["proxies"])
        self.assertIn("9.9.9.9:80", pm2._cache.proxy_groups["trusted"]["proxies"])

    def test_save_cache_persists(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080")
        pm.save_cache()
        pm2 = ProxyManager(cache_file=self.cache_file)
        self.assertIn("1.2.3.4:8080", pm2._cache.proxy_groups["default"]["proxies"])

    def test_get_trusted_proxies(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("trusted", "1.2.3.4:8080")
        pm._cache.add_to_group("trusted", "5.6.7.8:3128", fails=TRUSTED_MAX_FAILS)
        trusted = pm.get_trusted_proxies()
        self.assertIn("1.2.3.4:8080", trusted)
        self.assertNotIn("5.6.7.8:3128", trusted)

    def test_get_fail_count(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.add_to_group("default", "1.2.3.4:8080", fails=2)
        self.assertEqual(pm.get_fail_count("1.2.3.4:8080"), 2)
        self.assertEqual(pm.get_fail_count("0.0.0.0:0"), 0)

    def test_fetch_proxies_skips_paused_source(self):
        pm = ProxyManager(cache_file=self.cache_file)
        pm._cache.pause_source("proxy_pool.proxy-scdn-io")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 200, "data": {"proxies": ["5.6.7.8:3128"]}}
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertNotIn("5.6.7.8:3128", pm._cache.proxy_groups["default"]["proxies"])

    def test_fetch_proxies_dedup_across_sources(self):
        pm = ProxyManager(cache_file=self.cache_file)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 200, "data": {"proxies": ["1.2.3.4:8080"]}}
        mock_resp.raise_for_status = MagicMock()
        with patch("manager.requests.get", return_value=mock_resp):
            pm._fetch_proxies()
        self.assertIn("1.2.3.4:8080", pm._cache.proxy_groups["default"]["proxies"])


if __name__ == "__main__":
    unittest.main()
