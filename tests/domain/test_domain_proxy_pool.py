"""ProxyPool 领域模型测试（原 test_cache.py 规则部分，38 用例）"""

import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.framework.config import (
    MAX_FAILS,
    TRUSTED_MAX_FAILS,
    SOURCE_PAUSE_TTL,
    PROXY_GROUPS,
    SOURCES,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
)


def make_pool():
    """构造默认配置的 ProxyPool（等价组合根的行为）"""
    return ProxyPool(
        PROXY_GROUPS,
        max_fails=MAX_FAILS,
        trusted_max_fails=TRUSTED_MAX_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "freeproxy-world-http", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


class TestProxyPool(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()

    def test_groups_initialized(self):
        for name in PROXY_GROUPS:
            self.assertIn(name, self.pool.proxy_groups)

    def test_add_to_group(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["fails"], 0)

    def test_add_to_group_nonexistent(self):
        self.pool.add_to_group("nonexistent", "http://1.2.3.4:8080")
        self.assertNotIn("nonexistent", self.pool.proxy_groups)

    def test_add_to_group_custom_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=3)
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["fails"], 3)

    def test_add_to_group_joined_at(self):
        before = int(time.time())
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        after = int(time.time())
        joined = self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["joined_at"]
        self.assertGreaterEqual(joined, before)
        self.assertLessEqual(joined, after)

    def test_find_proxy(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        group, info = self.pool.find_proxy("http://1.2.3.4:8080")
        self.assertEqual(group, "default-http")
        self.assertEqual(info["fails"], 0)

    def test_find_proxy_not_found(self):
        group, info = self.pool.find_proxy("http://0.0.0.0:0")
        self.assertIsNone(group)
        self.assertIsNone(info)

    def test_find_proxy_expired_returns_none(self):
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "fails": 0
        }
        group, info = self.pool.find_proxy("http://1.2.3.4:8080")
        self.assertIsNone(group)
        self.assertIsNone(info)
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])

    def test_find_proxy_trusted_never_expires(self):
        self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        group, info = self.pool.find_proxy("http://1.2.3.4:8080")
        self.assertEqual(group, "trusted")

    def test_move_proxy(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=1)
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted", fails=0)
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["fails"], 0)

    def test_move_proxy_preserves_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=2)
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted")
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["fails"], 2)

    def test_move_proxy_from_nonexistent(self):
        self.pool.move_proxy("http://0.0.0.0:0", "trusted")
        self.assertIn("http://0.0.0.0:0", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://0.0.0.0:0"]["fails"], 0)

    def test_is_blacklisted(self):
        self.pool.add_to_group("blacklist", "http://1.2.3.4:8080")
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertFalse(self.pool.is_blacklisted("http://5.6.7.8:3128"))

    def test_is_blacklisted_expired(self):
        self.pool.proxy_groups["blacklist"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["blacklist"] - 1, "fails": 0
        }
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_is_known_proxy(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.assertTrue(self.pool.is_known_proxy("http://1.2.3.4:8080"))
        self.assertFalse(self.pool.is_known_proxy("http://5.6.7.8:3128"))

    def test_is_known_proxy_blacklisted(self):
        self.pool.add_to_group("blacklist", "http://1.2.3.4:8080")
        self.assertTrue(self.pool.is_known_proxy("http://1.2.3.4:8080"))

    def test_is_known_proxy_default_not_known(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.assertFalse(self.pool.is_known_proxy("http://1.2.3.4:8080"))

    def test_get_available_proxies(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("freeproxy-world-http", "http://5.6.7.8:3128")
        self.pool.add_to_group("trusted", "http://9.9.9.9:80")
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)
        self.assertIn("http://5.6.7.8:3128", available)
        self.assertIn("http://9.9.9.9:80", available)

    def test_get_available_excludes_blacklisted(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("blacklist", "http://1.2.3.4:8080")
        del self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_get_available_excludes_over_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=MAX_FAILS)
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_get_available_excludes_trusted_over_fails(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", fails=TRUSTED_MAX_FAILS)
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_get_available_excludes_specified(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        available = self.pool.get_available_proxies(exclude={"http://1.2.3.4:8080"})
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_get_available_dedup_across_groups(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        del self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)
        self.assertEqual(available["http://1.2.3.4:8080"]["group"], "trusted")

    def test_get_available_returns_group_info(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=1)
        available = self.pool.get_available_proxies()
        self.assertEqual(available["http://1.2.3.4:8080"]["group"], "default-http")
        self.assertEqual(available["http://1.2.3.4:8080"]["fails"], 1)

    def test_default_group_expires(self):
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "fails": 0
        }
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_freeproxy_world_group_not_expired(self):
        self.pool.add_to_group("freeproxy-world-http", "http://1.2.3.4:8080")
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)

    def test_trusted_group_never_expires(self):
        self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)

    def test_get_trusted_proxies(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", fails=0)
        self.pool.add_to_group("trusted", "http://5.6.7.8:3128", fails=TRUSTED_MAX_FAILS)
        trusted = self.pool.get_trusted_proxies()
        self.assertIn("http://1.2.3.4:8080", trusted)
        self.assertNotIn("http://5.6.7.8:3128", trusted)

    def test_get_fail_count(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=2)
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 2)
        self.assertEqual(self.pool.get_fail_count("http://0.0.0.0:0"), 0)

    def test_should_blacklist_normal_group_at_max_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=MAX_FAILS)
        self.assertTrue(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_normal_group_below_threshold(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=MAX_FAILS - 1)
        self.assertFalse(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_trusted_tolerates_more(self):
        # trusted 容错更高：普通组已触发的失败数，在 trusted 组仍不拉黑
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", fails=MAX_FAILS)
        self.assertFalse(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_trusted_at_trusted_max(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", fails=TRUSTED_MAX_FAILS)
        self.assertTrue(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_unknown_proxy(self):
        self.assertFalse(self.pool.should_blacklist("http://0.0.0.0:0"))

    def test_source_pause_custom_ttl(self):
        self.pool.pause_source("src.interfaces.sources.freeproxy_world", ttl=SOURCES[4]["pause_ttl"])
        self.assertTrue(self.pool.is_source_paused("src.interfaces.sources.freeproxy_world"))
        self.assertEqual(self.pool.source_paused["src.interfaces.sources.freeproxy_world"]["ttl"], SOURCES[4]["pause_ttl"])

    def test_source_pause_default_ttl(self):
        self.pool.pause_source("src.interfaces.sources.ip89")
        self.assertTrue(self.pool.is_source_paused("src.interfaces.sources.ip89"))
        self.assertIsNone(self.pool.source_paused["src.interfaces.sources.ip89"]["ttl"])

    def test_source_pause_expires(self):
        self.pool.source_paused["src.interfaces.sources.ip89"] = {"paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None}
        self.assertFalse(self.pool.is_source_paused("src.interfaces.sources.ip89"))
        self.assertNotIn("src.interfaces.sources.ip89", self.pool.source_paused)

    def test_source_pause_not_paused(self):
        self.assertFalse(self.pool.is_source_paused("nonexistent"))

    def test_source_pause_remaining(self):
        self.pool.pause_source("src.interfaces.sources.ip89")
        remaining = self.pool.source_pause_remaining("src.interfaces.sources.ip89")
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, SOURCE_PAUSE_TTL)

    def test_source_pause_remaining_none_when_not_paused(self):
        self.assertIsNone(self.pool.source_pause_remaining("nonexistent"))

    def test_source_pause_remaining_expires_and_removes(self):
        self.pool.source_paused["src.interfaces.sources.ip89"] = {"paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None}
        self.assertIsNone(self.pool.source_pause_remaining("src.interfaces.sources.ip89"))
        self.assertNotIn("src.interfaces.sources.ip89", self.pool.source_paused)

    def test_remove_all_copies(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.pool.remove_all_copies("http://1.2.3.4:8080", BLACKLIST_GROUP)
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_remove_all_copies_nonexistent(self):
        self.pool.remove_all_copies("http://0.0.0.0:0", BLACKLIST_GROUP)
        self.assertTrue(self.pool.is_blacklisted("http://0.0.0.0:0"))

    def test_cleanup_all_expired(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://9.9.9.9:80")
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["joined_at"] = time.time() - PROXY_GROUPS["default-http"] - 1
        self.pool._cleanup_all_expired()
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://9.9.9.9:80", self.pool.proxy_groups["trusted"]["proxies"])

    def test_cleanup_skips_trusted(self):
        self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "fails": 0
        }
        self.pool._cleanup_all_expired()
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])

    def test_multiple_proxies_in_group(self):
        for i in range(10):
            self.pool.add_to_group("default-http", f"http://{i}.0.0.0:8080")
        available = self.pool.get_available_proxies()
        self.assertEqual(len(available), 10)

    def test_is_active_nonexistent_group(self):
        self.assertFalse(self.pool._is_active("nonexistent", "http://1.2.3.4:8080"))

    def test_is_active_proxy_not_in_group(self):
        self.assertFalse(self.pool._is_active("default-http", "http://1.2.3.4:8080"))


if __name__ == "__main__":
    unittest.main()
