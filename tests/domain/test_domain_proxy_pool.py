"""ProxyPool 领域模型测试（原 test_cache.py 规则部分，38 用例）"""

import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool, LOCKED_GROUP
from src.framework.config import (
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
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
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
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
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 0)

    def test_add_to_group_nonexistent(self):
        self.pool.add_to_group("nonexistent", "http://1.2.3.4:8080")
        self.assertNotIn("nonexistent", self.pool.proxy_groups)

    def test_add_to_group_rejects_unsupported_scheme(self):
        """scheme 不在 SUPPORTED_SCHEMES（socks4）：入池被拒（返回 False），不写入组"""
        self.assertFalse(self.pool.add_to_group("default-http", "socks4://1.2.3.4:1080"))
        self.assertNotIn("socks4://1.2.3.4:1080",
                         self.pool.proxy_groups["default-http"]["proxies"])
        self.assertTrue(self.pool.add_to_group("default-http", "socks5://1.2.3.4:1080"))

    def test_restore_state_drops_unsupported_scheme(self):
        """历史入库的 socks4 条目恢复时被丢弃并计数（协议集合收窄后的迁移语义）"""
        now = int(time.time())
        data = {"proxy_groups": {"default-http": {"ttl": 3600, "proxies": [
            {"proxy": "socks4://1.2.3.4:1080", "joined_at": now, "consecutive_fails": 0},
            {"proxy": "http://5.6.7.8:8080", "joined_at": now, "consecutive_fails": 0},
        ]}}}
        self.assertEqual(self.pool.restore_state(data), 1)
        self.assertIn("http://5.6.7.8:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertNotIn("socks4://1.2.3.4:1080",
                         self.pool.proxy_groups["default-http"]["proxies"])

    def test_add_to_group_custom_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=3)
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 3)

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
        self.assertEqual(info["consecutive_fails"], 0)

    def test_find_proxy_not_found(self):
        group, info = self.pool.find_proxy("http://0.0.0.0:0")
        self.assertIsNone(group)
        self.assertIsNone(info)

    def test_find_proxy_expired_returns_none(self):
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "consecutive_fails": 0
        }
        group, info = self.pool.find_proxy("http://1.2.3.4:8080")
        self.assertIsNone(group)
        self.assertIsNone(info)
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])

    def test_find_proxy_trusted_never_expires(self):
        self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "consecutive_fails": 0
        }
        group, info = self.pool.find_proxy("http://1.2.3.4:8080")
        self.assertEqual(group, "trusted")

    def test_move_proxy(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=1)
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted", consecutive_fails=0)
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 0)

    def test_move_proxy_preserves_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=2)
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted")
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 2)

    def test_move_proxy_from_nonexistent(self):
        self.pool.move_proxy("http://0.0.0.0:0", "trusted")
        self.assertIn("http://0.0.0.0:0", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://0.0.0.0:0"]["consecutive_fails"], 0)

    def test_is_blacklisted(self):
        self.pool.add_to_group("blacklist", "http://1.2.3.4:8080")
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertFalse(self.pool.is_blacklisted("http://5.6.7.8:3128"))

    def test_is_blacklisted_expired(self):
        self.pool.proxy_groups["blacklist"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["blacklist"] - 1, "consecutive_fails": 0
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

    def test_get_available_excludes_over_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS)
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_get_available_excludes_trusted_over_consecutive_fails(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS)
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
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=1)
        available = self.pool.get_available_proxies()
        self.assertEqual(available["http://1.2.3.4:8080"]["group"], "default-http")
        self.assertEqual(available["http://1.2.3.4:8080"]["consecutive_fails"], 1)

    def test_default_group_expires(self):
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "consecutive_fails": 0
        }
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)

    def test_freeproxy_world_group_not_expired(self):
        self.pool.add_to_group("freeproxy-world-http", "http://1.2.3.4:8080")
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)

    def test_trusted_group_never_expires(self):
        self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - 999999, "consecutive_fails": 0
        }
        available = self.pool.get_available_proxies()
        self.assertIn("http://1.2.3.4:8080", available)

    def test_get_trusted_proxies(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.add_to_group("trusted", "http://5.6.7.8:3128", consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS)
        trusted = self.pool.get_trusted_proxies()
        self.assertIn("http://1.2.3.4:8080", trusted)
        self.assertNotIn("http://5.6.7.8:3128", trusted)

    def test_get_consecutive_fail_count(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=2)
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 2)
        self.assertEqual(self.pool.get_consecutive_fail_count("http://0.0.0.0:0"), 0)

    def test_should_blacklist_normal_group_at_max_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS)
        self.assertTrue(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_normal_group_below_threshold(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS - 1)
        self.assertFalse(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_trusted_tolerates_more(self):
        # trusted 容错更高：普通组已触发的失败数，在 trusted 组仍不拉黑
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS)
        self.assertFalse(self.pool.should_blacklist("http://1.2.3.4:8080"))

    def test_should_blacklist_trusted_at_trusted_max(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS)
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

    def test_remove_all_copies_records_source_group(self):
        """拉黑时记录来源组（find_proxy 定位的活跃组），供前端展示"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.remove_all_copies("http://1.2.3.4:8080", BLACKLIST_GROUP)
        info = self.pool.proxy_groups[BLACKLIST_GROUP]["proxies"]["http://1.2.3.4:8080"]
        self.assertEqual(info["source_group"], "default-http")

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
            "joined_at": time.time() - 999999, "consecutive_fails": 0
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


class TestProxyPoolPinned(unittest.TestCase):
    """固定代理（pin/unpin）领域规则测试"""

    def setUp(self):
        self.pool = make_pool()

    def test_pin_unpin_basic(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.assertFalse(self.pool.is_pinned("http://1.2.3.4:8080"))
        self.pool.pin("http://1.2.3.4:8080")
        self.assertTrue(self.pool.is_pinned("http://1.2.3.4:8080"))
        self.assertIsNotNone(self.pool.get_pinned_at("http://1.2.3.4:8080"))
        self.pool.unpin("http://1.2.3.4:8080")
        self.assertFalse(self.pool.is_pinned("http://1.2.3.4:8080"))
        self.assertIsNone(self.pool.get_pinned_at("http://1.2.3.4:8080"))

    def test_pin_nonexistent_no_op(self):
        self.pool.pin("http://9.9.9.9:9999")
        self.assertFalse(self.pool.is_pinned("http://9.9.9.9:9999"))

    def test_move_preserves_pinned(self):
        """move_proxy 透传 pinned/pinned_at（避免移动丢失固定状态）"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.pin("http://1.2.3.4:8080")
        pinned_at = self.pool.get_pinned_at("http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted", consecutive_fails=0)
        self.assertTrue(self.pool.is_pinned("http://1.2.3.4:8080"))
        self.assertEqual(self.pool.get_pinned_at("http://1.2.3.4:8080"), pinned_at)

    def test_export_restore_preserves_pinned(self):
        """序列化透传 pinned/pinned_at（重启后恢复固定状态）"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        self.pool.pin("http://1.2.3.4:8080")
        state = self.pool.export_state()
        # 固定代理导出含 pinned/pinned_at
        exported = {e["proxy"]: e for e in state["proxy_groups"]["default-http"]["proxies"]}
        self.assertTrue(exported["http://1.2.3.4:8080"].get("pinned"))
        self.assertIn("pinned_at", exported["http://1.2.3.4:8080"])
        self.assertNotIn("pinned", exported["http://5.6.7.8:3128"])
        # 恢复到新池
        pool2 = make_pool()
        pool2.restore_state(state)
        self.assertTrue(pool2.is_pinned("http://1.2.3.4:8080"))
        self.assertFalse(pool2.is_pinned("http://5.6.7.8:3128"))

    def test_export_restore_preserves_source_group(self):
        """序列化透传 source_group（重启后黑名单代理仍记来源组）"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.remove_all_copies("http://1.2.3.4:8080", BLACKLIST_GROUP)
        state = self.pool.export_state()
        exported = {e["proxy"]: e for e in state["proxy_groups"][BLACKLIST_GROUP]["proxies"]}
        self.assertEqual(exported["http://1.2.3.4:8080"].get("source_group"), "default-http")
        # 恢复到新池
        pool2 = make_pool()
        pool2.restore_state(state)
        info = pool2.proxy_groups[BLACKLIST_GROUP]["proxies"]["http://1.2.3.4:8080"]
        self.assertEqual(info["source_group"], "default-http")


class TestProxyPoolLocked(unittest.TestCase):
    """内建锁定组（LOCKED_GROUP）领域规则测试"""

    def setUp(self):
        self.pool = make_pool()

    def test_locked_group_builtin(self):
        """LOCKED_GROUP 在 __init__ 自动创建，ttl=None 永不过期，不在 config.toml"""
        self.assertIn(LOCKED_GROUP, self.pool.proxy_groups)
        self.assertIsNone(self.pool.proxy_groups[LOCKED_GROUP]["ttl"])

    def test_export_restore_preserves_locked(self):
        """锁定组代理经 export/restore 持久化（重启后恢复锁定状态）"""
        self.pool.add_to_group(LOCKED_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        state = self.pool.export_state()
        self.assertIn(LOCKED_GROUP, state["proxy_groups"])
        exported = [e["proxy"] for e in state["proxy_groups"][LOCKED_GROUP]["proxies"]]
        self.assertIn("http://1.2.3.4:8080", exported)
        # 恢复到新池
        pool2 = make_pool()
        pool2.restore_state(state)
        self.assertIn("http://1.2.3.4:8080", pool2.get_group_proxies(LOCKED_GROUP))

    def test_locked_group_not_requestable(self):
        """LOCKED_GROUP 不在 requestable_groups，不参与可用集合（锁定代理不被其他命令取走）"""
        available = self.pool.get_available_proxies()
        self.assertNotIn(LOCKED_GROUP, {info["group"] for info in available.values()})
        self.pool.add_to_group(LOCKED_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        available = self.pool.get_available_proxies()
        self.assertNotIn("http://1.2.3.4:8080", available)


class TestProxyPoolDirtyTracking(unittest.TestCase):
    """dirty tracking：修改方法标记 dirty 组，Container.save 据此增量持久化"""

    def setUp(self):
        self.pool = make_pool()

    def test_initial_no_dirty(self):
        self.assertEqual(self.pool.dirty_groups(), set())
        self.assertFalse(self.pool.source_paused_dirty())

    def test_add_to_group_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_move_proxy_marks_dirty(self):
        """move_proxy 标记源组 + 目标组"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.clear_dirty()
        self.pool.move_proxy("http://1.2.3.4:8080", "trusted")
        self.assertEqual(self.pool.dirty_groups(), {"default-http", "trusted"})

    def test_pin_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.clear_dirty()
        self.pool.pin("http://1.2.3.4:8080")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_unpin_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.pin("http://1.2.3.4:8080")
        self.pool.clear_dirty()
        self.pool.unpin("http://1.2.3.4:8080")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_update_latency_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.clear_dirty()
        self.pool.update_latency("http://1.2.3.4:8080", 0.5)
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_remove_all_copies_marks_dirty(self):
        """remove_all_copies 标记所有含该代理的组 + blacklist"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.pool.clear_dirty()
        self.pool.remove_all_copies("http://1.2.3.4:8080", BLACKLIST_GROUP)
        dirty = self.pool.dirty_groups()
        self.assertIn("default-http", dirty)
        self.assertIn("trusted", dirty)
        self.assertIn(BLACKLIST_GROUP, dirty)

    def test_reset_consecutive_fails_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=2)
        self.pool.clear_dirty()
        self.pool.reset_consecutive_fails("http://1.2.3.4:8080")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_reset_consecutive_fails_with_group_marks_dirty(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=2)
        self.pool.clear_dirty()
        self.pool.reset_consecutive_fails("http://1.2.3.4:8080", group="default-http")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_pause_source_marks_dirty(self):
        self.pool.pause_source("src.interfaces.sources.ip89")
        self.assertTrue(self.pool.source_paused_dirty())

    def test_clear_dirty(self):
        """clear_dirty 后 dirty_groups 为空"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.pause_source("src.interfaces.sources.ip89")
        self.pool.clear_dirty()
        self.assertEqual(self.pool.dirty_groups(), set())
        self.assertFalse(self.pool.source_paused_dirty())

    def test_dirty_groups_returns_copy(self):
        """dirty_groups 返回副本（外部修改不影响内部）"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        dirty = self.pool.dirty_groups()
        dirty.add("trusted")
        self.assertEqual(self.pool.dirty_groups(), {"default-http"})

    def test_export_group(self):
        """export_group 只含该组代理"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", latency=0.5)
        self.pool.add_to_group("trusted", "http://9.9.9.9:80")
        exported = self.pool.export_group("default-http")
        self.assertEqual(len(exported["proxies"]), 1)
        self.assertEqual(exported["proxies"][0]["proxy"], "http://1.2.3.4:8080")
        self.assertEqual(exported["proxies"][0]["latency"], 0.5)
        self.assertEqual(exported["ttl"], PROXY_GROUPS["default-http"])

    def test_export_group_nonexistent(self):
        exported = self.pool.export_group("nonexistent")
        self.assertEqual(exported, {"ttl": None, "proxies": []})

    def test_export_group_filters_expired(self):
        """export_group 不导出过期代理（但不从内存删除）"""
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "consecutive_fails": 0
        }
        exported = self.pool.export_group("default-http")
        self.assertEqual(exported["proxies"], [])
        # 内存未删除（清理由 _cleanup_all_expired 负责）
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])

    def test_export_source_paused(self):
        self.pool.pause_source("src.interfaces.sources.ip89")
        exported = self.pool.export_source_paused()
        self.assertIn("src.interfaces.sources.ip89", exported)

    def test_cleanup_marks_dirty(self):
        """过期清理标记被清理了代理的组"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://9.9.9.9:80")
        self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["joined_at"] = (
            time.time() - PROXY_GROUPS["default-http"] - 1
        )
        self.pool.clear_dirty()
        self.pool._last_cleanup = 0  # 重置节流以触发清理
        self.pool._cleanup_all_expired()
        self.assertIn("default-http", self.pool.dirty_groups())
        self.assertNotIn("trusted", self.pool.dirty_groups())


if __name__ == "__main__":
    unittest.main()
