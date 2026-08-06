"""StandbyQueueUseCase 单元测试：fill 补入 / heartbeat 计次淘汰 / 退避补测

探测（check_proxies）全程 mock，不依赖网络。
"""

import asyncio
import unittest
from unittest.mock import MagicMock

from src.domain.proxy_pool import ProxyPool
from src.framework.config import (
    PROXY_GROUPS, MAX_FAILS, TRUSTED_MAX_FAILS, SOURCE_PAUSE_TTL,
    TRUSTED_GROUP, BLACKLIST_GROUP, STANDBY_GROUP,
)
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.standby import StandbyQueueUseCase


def make_pool():
    return ProxyPool(
        PROXY_GROUPS,
        max_fails=MAX_FAILS,
        trusted_max_fails=TRUSTED_MAX_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "default-https", "default-socks5",
                            "freeproxy-world-http", "freeproxy-world-https",
                            "freeproxy-world-socks5", STANDBY_GROUP, TRUSTED_GROUP),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


def make_check(success_map):
    """探测 mock：success_map 内成功，其余失败（async 签名与 CheckProxiesUseCase 一致）"""
    check = MagicMock()

    async def execute(proxies, url, **kw):
        return _async_result({p: p in success_map for p in proxies})

    check.execute.side_effect = execute
    return check


def _async_result(success_map):
    """构造探测结果 dict：success_map 内成功，其余失败"""
    return {
        p: {"success": ok, "status_code": 200 if ok else None,
            "elapsed": 0.1, "error": None if ok else "Timeout"}
        for p, ok in success_map.items()
    }


def make_standby(pool, check, fill_groups=("default-http",), multiplier=2, min_extra=1):
    return StandbyQueueUseCase(
        pool=pool,
        record=RecordProxyUseCase(pool=pool, standby_group=STANDBY_GROUP),
        check_proxies=check,
        standby_group=STANDBY_GROUP,
        fill_groups=fill_groups,
        fill_probe_multiplier=multiplier,
        fill_probe_min_extra=min_extra,
    )


class TestStandbyFill(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()

    def test_fill_moves_alive_proxies_blacklists_dead(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 1)
        self.assertEqual(c.standby_proxies(), ["http://1.2.3.4:8080"])
        self.assertTrue(self.pool.is_blacklisted("http://5.6.7.8:3128"))
        # 入队失败计数清零
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 0)

    def test_fill_skips_existing_standby_members(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, fails=0)
        calls = []
        check = MagicMock()

        async def execute(proxies, url, **kw):
            calls.append(list(proxies))
            return {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}

        check.execute.side_effect = execute
        c = make_standby(self.pool, check)
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 0)
        # 队列内代理全部排除 -> 无候选，不发起探测
        self.assertEqual(calls, [])

    def test_fill_skips_wrong_protocol(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-socks5", "socks5://5.6.7.8:3128")
        check = MagicMock()

        async def execute(proxies, url, **kw):
            return {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}

        check.execute.side_effect = execute
        c = make_standby(self.pool, check, fill_groups=("default-http", "default-socks5"))
        asyncio.run(c.fill({"socks5"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(c.standby_proxies(), ["socks5://5.6.7.8:3128"])

    def test_fill_caps_candidates_within_single_group(self):
        """单个组候选超过 probe_count 时必须截断，不能整组全量探测（满池回归）"""
        for i in range(50):
            self.pool.add_to_group("default-http", f"http://10.0.0.{i}:8080")
        calls = []
        check = MagicMock()

        async def execute(proxies, url, **kw):
            calls.append(list(proxies))
            return {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}

        check.execute.side_effect = execute
        # capacity=5, multiplier=2, min_extra=1 -> probe_count = max(10, 6) = 10
        c = make_standby(self.pool, check, fill_groups=("default-http",))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 5)
        self.assertEqual(len(calls[0]), 10)  # 只探测 10 个候选，而不是整组 50 个

    def test_fill_no_candidates_when_queue_full(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, fails=0)
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=1))
        self.assertEqual(added, 0)

    def test_fill_candidate_count_uses_multiplier(self):
        for i in range(3):
            self.pool.add_to_group("default-http", f"http://1.0.0.{i}:8080")
        check = MagicMock()

        async def execute(proxies, url, **kw):
            return {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}

        check.execute.side_effect = execute
        c = make_standby(self.pool, check, multiplier=2, min_extra=0)
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        # 缺口 5 → 候选 5×2=10，池内仅 3 个候选
        probed = check.execute.call_args_list[0].args[0]
        self.assertEqual(len(probed), 3)


class TestStandbyHeartbeat(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, fails=0)

    def test_heartbeat_success_resets_fails_keeps_in_queue(self):
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://1.2.3.4:8080"]["fails"] = 1
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, ["http://1.2.3.4:8080"])
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 0)
        self.assertEqual(c.pending_retries(), [])

    def test_heartbeat_first_failure_enters_retry_not_blacklisted(self):
        c = make_standby(self.pool, make_check(set()))
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, [])
        # 一次失败：计 1 次，未达阈值（MAX_FAILS=2），进入退避补测，仍在队列
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 1)
        self.assertEqual(c.pending_retries(), ["http://1.2.3.4:8080"])
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_heartbeat_skips_proxies_in_retry(self):
        check = MagicMock()
        c = make_standby(self.pool, check)
        c._retrying.add("http://1.2.3.4:8080")
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        check.execute.assert_not_called()

    def test_heartbeat_empty_queue_skips(self):
        self.pool.move_proxy("http://1.2.3.4:8080", self.pool.blacklist_group)
        check = MagicMock()
        c = make_standby(self.pool, check)
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, [])
        check.execute.assert_not_called()


class TestStandbyRetryProbe(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, fails=0)

    def test_retry_success_recovers(self):
        check = make_check(set())
        c = make_standby(self.pool, check)
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(c.pending_retries(), ["http://1.2.3.4:8080"])
        # 补测期探测恢复正常
        async def execute(proxies, url, **kw):
            return _async_result({p: True for p in proxies})

        check.execute.side_effect = execute
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 0)
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertEqual(c.pending_retries(), [])

    def test_retry_failure_blacklists_at_threshold(self):
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        # 心跳 1 次 + 补测 1 次 = 2 = MAX_FAILS -> 拉黑移出
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertNotIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertEqual(c.pending_retries(), [])

    def test_retry_stops_when_proxy_leaves_queue(self):
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.pool.move_proxy("http://1.2.3.4:8080", "default-http", fails=0)
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        self.assertEqual(c.pending_retries(), [])
        self.assertNotIn("http://1.2.3.4:8080", c.standby_proxies())


if __name__ == "__main__":
    unittest.main()
