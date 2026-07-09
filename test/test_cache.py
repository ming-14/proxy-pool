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

from cache import CacheManager
from config import MAX_FAILS, TRUSTED_MAX_FAILS, SOURCE_PAUSE_TTL, PROXY_GROUPS, SOURCES as _SOURCES

SOURCES = [{**s, "enabled": True} for s in _SOURCES]


class TestCacheManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_groups_initialized(self):
        cm = CacheManager(cache_file=self.cache_file)
        for name in PROXY_GROUPS:
            self.assertIn(name, cm.proxy_groups)

    def test_add_to_group(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        self.assertIn("1.2.3.4:8080", cm.proxy_groups["default"]["proxies"])
        self.assertEqual(cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["fails"], 0)

    def test_add_to_group_nonexistent(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("nonexistent", "1.2.3.4:8080")
        self.assertNotIn("nonexistent", cm.proxy_groups)

    def test_add_to_group_custom_fails(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=3)
        self.assertEqual(cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["fails"], 3)

    def test_add_to_group_joined_at(self):
        cm = CacheManager(cache_file=self.cache_file)
        before = int(time.time())
        cm.add_to_group("default", "1.2.3.4:8080")
        after = int(time.time())
        joined = cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["joined_at"]
        self.assertGreaterEqual(joined, before)
        self.assertLessEqual(joined, after)

    def test_find_proxy(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        group, info = cm.find_proxy("1.2.3.4:8080")
        self.assertEqual(group, "default")
        self.assertEqual(info["fails"], 0)

    def test_find_proxy_not_found(self):
        cm = CacheManager(cache_file=self.cache_file)
        group, info = cm.find_proxy("0.0.0.0:0")
        self.assertIsNone(group)
        self.assertIsNone(info)

    def test_find_proxy_expired_returns_none(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default"] - 1, "fails": 0
        }
        group, info = cm.find_proxy("1.2.3.4:8080")
        self.assertIsNone(group)
        self.assertIsNone(info)
        self.assertNotIn("1.2.3.4:8080", cm.proxy_groups["default"]["proxies"])

    def test_find_proxy_trusted_never_expires(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        group, info = cm.find_proxy("1.2.3.4:8080")
        self.assertEqual(group, "trusted")

    def test_move_proxy(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=1)
        cm.move_proxy("1.2.3.4:8080", "trusted", fails=0)
        self.assertNotIn("1.2.3.4:8080", cm.proxy_groups["default"]["proxies"])
        self.assertIn("1.2.3.4:8080", cm.proxy_groups["trusted"]["proxies"])
        self.assertEqual(cm.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"]["fails"], 0)

    def test_move_proxy_preserves_fails(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=2)
        cm.move_proxy("1.2.3.4:8080", "trusted")
        self.assertEqual(cm.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"]["fails"], 2)

    def test_move_proxy_from_nonexistent(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.move_proxy("0.0.0.0:0", "trusted")
        self.assertIn("0.0.0.0:0", cm.proxy_groups["trusted"]["proxies"])
        self.assertEqual(cm.proxy_groups["trusted"]["proxies"]["0.0.0.0:0"]["fails"], 0)

    def test_is_blacklisted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("blacklist", "1.2.3.4:8080")
        self.assertTrue(cm.is_blacklisted("1.2.3.4:8080"))
        self.assertFalse(cm.is_blacklisted("5.6.7.8:3128"))

    def test_is_blacklisted_expired(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["blacklist"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["blacklist"] - 1, "fails": 0
        }
        self.assertFalse(cm.is_blacklisted("1.2.3.4:8080"))

    def test_is_known_proxy(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("trusted", "1.2.3.4:8080")
        self.assertTrue(cm.is_known_proxy("1.2.3.4:8080"))
        self.assertFalse(cm.is_known_proxy("5.6.7.8:3128"))

    def test_is_known_proxy_blacklisted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("blacklist", "1.2.3.4:8080")
        self.assertTrue(cm.is_known_proxy("1.2.3.4:8080"))

    def test_is_known_proxy_default_not_known(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        self.assertFalse(cm.is_known_proxy("1.2.3.4:8080"))

    def test_get_available_proxies(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("freeproxy-world", "5.6.7.8:3128")
        cm.add_to_group("trusted", "9.9.9.9:80")
        available = cm.get_available_proxies()
        self.assertIn("1.2.3.4:8080", available)
        self.assertIn("5.6.7.8:3128", available)
        self.assertIn("9.9.9.9:80", available)

    def test_get_available_excludes_blacklisted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("blacklist", "1.2.3.4:8080")
        del cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]
        available = cm.get_available_proxies()
        self.assertNotIn("1.2.3.4:8080", available)

    def test_get_available_excludes_over_fails(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=MAX_FAILS)
        available = cm.get_available_proxies()
        self.assertNotIn("1.2.3.4:8080", available)

    def test_get_available_excludes_trusted_over_fails(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("trusted", "1.2.3.4:8080", fails=TRUSTED_MAX_FAILS)
        available = cm.get_available_proxies()
        self.assertNotIn("1.2.3.4:8080", available)

    def test_get_available_excludes_specified(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        available = cm.get_available_proxies(exclude={"1.2.3.4:8080"})
        self.assertNotIn("1.2.3.4:8080", available)

    def test_get_available_dedup_across_groups(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("trusted", "1.2.3.4:8080")
        del cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]
        available = cm.get_available_proxies()
        self.assertIn("1.2.3.4:8080", available)
        self.assertEqual(available["1.2.3.4:8080"]["group"], "trusted")

    def test_get_available_returns_group_info(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=1)
        available = cm.get_available_proxies()
        self.assertEqual(available["1.2.3.4:8080"]["group"], "default")
        self.assertEqual(available["1.2.3.4:8080"]["fails"], 1)

    def test_default_group_expires(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default"] - 1, "fails": 0
        }
        available = cm.get_available_proxies()
        self.assertNotIn("1.2.3.4:8080", available)

    def test_freeproxy_world_group_not_expired(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("freeproxy-world", "1.2.3.4:8080")
        available = cm.get_available_proxies()
        self.assertIn("1.2.3.4:8080", available)

    def test_trusted_group_never_expires(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        available = cm.get_available_proxies()
        self.assertIn("1.2.3.4:8080", available)

    def test_save_and_load(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=1)
        cm.add_to_group("freeproxy-world", "5.6.7.8:3128")
        cm.add_to_group("trusted", "9.9.9.9:80", fails=2)
        cm.add_to_group("blacklist", "10.10.10.10:80")
        cm.save()

        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertIn("1.2.3.4:8080", cm2.proxy_groups["default"]["proxies"])
        self.assertIn("5.6.7.8:3128", cm2.proxy_groups["freeproxy-world"]["proxies"])
        self.assertIn("9.9.9.9:80", cm2.proxy_groups["trusted"]["proxies"])
        self.assertIn("10.10.10.10:80", cm2.proxy_groups["blacklist"]["proxies"])

    def test_load_filters_expired(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default"] - 1, "fails": 0
        }
        cm.add_to_group("trusted", "9.9.9.9:80")
        cm.save()

        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertNotIn("1.2.3.4:8080", cm2.proxy_groups["default"]["proxies"])
        self.assertIn("9.9.9.9:80", cm2.proxy_groups["trusted"]["proxies"])

    def test_load_corrupt_file(self):
        with open(self.cache_file, "w") as f:
            f.write("not valid json{{{")
        cm = CacheManager(cache_file=self.cache_file)
        for name in PROXY_GROUPS:
            self.assertIn(name, cm.proxy_groups)

    def test_load_nonexistent_file(self):
        cm = CacheManager(cache_file=os.path.join(self.tmp_dir, "no_such_file"))
        for name in PROXY_GROUPS:
            self.assertIn(name, cm.proxy_groups)

    def test_save_preserves_fails(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=2)
        cm.save()
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertEqual(cm2.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["fails"], 2)

    def test_get_trusted_proxies(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("trusted", "1.2.3.4:8080", fails=0)
        cm.add_to_group("trusted", "5.6.7.8:3128", fails=TRUSTED_MAX_FAILS)
        trusted = cm.get_trusted_proxies()
        self.assertIn("1.2.3.4:8080", trusted)
        self.assertNotIn("5.6.7.8:3128", trusted)

    def test_get_fail_count(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080", fails=2)
        self.assertEqual(cm.get_fail_count("1.2.3.4:8080"), 2)
        self.assertEqual(cm.get_fail_count("0.0.0.0:0"), 0)

    def test_source_pause_custom_ttl(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.pause_source("proxy_pool.freeproxy-world", ttl=SOURCES[4]["pause_ttl"])
        self.assertTrue(cm.is_source_paused("proxy_pool.freeproxy-world"))
        self.assertEqual(cm.source_paused["proxy_pool.freeproxy-world"]["ttl"], SOURCES[4]["pause_ttl"])

    def test_source_pause_default_ttl(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.pause_source("proxy_pool.89ip-cn")
        self.assertTrue(cm.is_source_paused("proxy_pool.89ip-cn"))
        self.assertIsNone(cm.source_paused["proxy_pool.89ip-cn"]["ttl"])

    def test_source_pause_expires(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.source_paused["proxy_pool.89ip-cn"] = {"paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None}
        self.assertFalse(cm.is_source_paused("proxy_pool.89ip-cn"))
        self.assertNotIn("proxy_pool.89ip-cn", cm.source_paused)

    def test_source_pause_not_paused(self):
        cm = CacheManager(cache_file=self.cache_file)
        self.assertFalse(cm.is_source_paused("nonexistent"))

    def test_source_pause_persisted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.pause_source("proxy_pool.89ip-cn")
        cm.save()
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertIn("proxy_pool.89ip-cn", cm2.source_paused)

    def test_source_pause_expired_not_persisted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.source_paused["proxy_pool.89ip-cn"] = {"paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None}
        cm.save()
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertNotIn("proxy_pool.89ip-cn", cm2.source_paused)

    def test_cleanup_all_expired(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("trusted", "9.9.9.9:80")
        cm.proxy_groups["default"]["proxies"]["1.2.3.4:8080"]["joined_at"] = time.time() - PROXY_GROUPS["default"] - 1
        cm._cleanup_all_expired()
        self.assertNotIn("1.2.3.4:8080", cm.proxy_groups["default"]["proxies"])
        self.assertIn("9.9.9.9:80", cm.proxy_groups["trusted"]["proxies"])

    def test_cleanup_skips_trusted(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.proxy_groups["trusted"]["proxies"]["1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        cm._cleanup_all_expired()
        self.assertIn("1.2.3.4:8080", cm.proxy_groups["trusted"]["proxies"])

    def test_multiple_proxies_in_group(self):
        cm = CacheManager(cache_file=self.cache_file)
        for i in range(10):
            cm.add_to_group("default", f"{i}.0.0.0:8080")
        available = cm.get_available_proxies()
        self.assertEqual(len(available), 10)

    def test_save_load_round_trip_many(self):
        cm = CacheManager(cache_file=self.cache_file)
        for i in range(50):
            cm.add_to_group("default", f"{i}.0.0.0:8080", fails=i % 3)
        cm.save()
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertEqual(len(cm2.proxy_groups["default"]["proxies"]), 50)

    def test_is_active_nonexistent_group(self):
        cm = CacheManager(cache_file=self.cache_file)
        self.assertFalse(cm._is_active("nonexistent", "1.2.3.4:8080"))

    def test_is_active_proxy_not_in_group(self):
        cm = CacheManager(cache_file=self.cache_file)
        self.assertFalse(cm._is_active("default", "1.2.3.4:8080"))


if __name__ == "__main__":
    unittest.main()
