"""StandbyQueueUseCase 单元测试：fill 流式补入/排序淘汰 / heartbeat 计次淘汰 / 退避补测

探测（check_proxies）全程 mock，不依赖网络。mock 需调 on_result 模拟流式回调。
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


def _fire_on_result(proxies, results, kw):
    """模拟 CheckProxiesUseCase 的 on_result 回调（fill 流式入队依赖）"""
    on_result = kw.get("on_result")
    if on_result:
        for p in proxies:
            on_result(p, results[p])


def make_check(success_map):
    """探测 mock：success_map 内成功，其余失败；调 on_result 模拟流式回调"""
    check = MagicMock()

    async def execute(proxies, url, **kw):
        results = _async_result({p: p in success_map for p in proxies})
        _fire_on_result(proxies, results, kw)
        return results

    check.execute.side_effect = execute
    return check


def make_check_results(spec):
    """探测 mock：spec = {proxy: {"success": bool, "elapsed": float}}，支持每代理不同 latency"""
    check = MagicMock()

    async def execute(proxies, url, **kw):
        results = {}
        for p in proxies:
            s = spec.get(p, {"success": False, "elapsed": 0.1})
            results[p] = {
                "success": s["success"],
                "status_code": 200 if s["success"] else None,
                "elapsed": s.get("elapsed", 0.1),
                "error": None if s["success"] else "Timeout",
            }
        _fire_on_result(proxies, results, kw)
        return results

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
        # 预设 fails=1，探测失败后 fails=2=MAX_FAILS -> 拉黑（fill 用 max_fails 机制，非单次拉黑）
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128", fails=1)
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 1)
        self.assertEqual(c.standby_proxies(), ["http://1.2.3.4:8080"])
        self.assertTrue(self.pool.is_blacklisted("http://5.6.7.8:3128"))
        # 入队失败计数清零
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 0)

    def test_fill_failure_records_fails_not_blacklist(self):
        """探测失败只 record_fail，未达 max_fails 不拉黑，留原组下次可再被选"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(self.pool.get_fail_count("http://1.2.3.4:8080"), 1)
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", self.pool.get_group_proxies("default-http"))

    def test_fill_skips_existing_standby_members(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, fails=0)
        calls = []
        check = MagicMock()

        async def execute(proxies, url, **kw):
            calls.append(list(proxies))
            results = {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}
            _fire_on_result(proxies, results, kw)
            return results

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
            results = {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}
            _fire_on_result(proxies, results, kw)
            return results

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
            results = {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}
            _fire_on_result(proxies, results, kw)
            return results

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
            results = {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}
            _fire_on_result(proxies, results, kw)
            return results

        check.execute.side_effect = execute
        c = make_standby(self.pool, check, multiplier=2, min_extra=0)
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        # 缺口 5 → 候选 5×2=10，池内仅 3 个候选
        probed = check.execute.call_args_list[0].args[0]
        self.assertEqual(len(probed), 3)

    def test_fill_orders_by_latency(self):
        """队列按 latency 升序排列（快的在前）"""
        proxies = [f"http://10.0.0.{i}:8080" for i in range(3)]
        for p in proxies:
            self.pool.add_to_group("default-http", p)
        # 3 个代理不同延迟：0.3s, 0.1s, 0.2s
        spec = {
            "http://10.0.0.0:8080": {"success": True, "elapsed": 0.3},
            "http://10.0.0.1:8080": {"success": True, "elapsed": 0.1},
            "http://10.0.0.2:8080": {"success": True, "elapsed": 0.2},
        }
        c = make_standby(self.pool, make_check_results(spec), fill_groups=("default-http",), multiplier=1, min_extra=0)
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=3, capacity=5))
        # 按 latency 升序：0.1 < 0.2 < 0.3
        self.assertEqual(c.standby_proxies(), [
            "http://10.0.0.1:8080",
            "http://10.0.0.2:8080",
            "http://10.0.0.0:8080",
        ])

    def test_fill_replaces_slower_tail(self):
        """队列有缺口时探测，填满后后续更快代理踢队尾 move trusted"""
        # 预填队列 1 个慢代理 latency=2.0s（留 1 个缺口）
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.0:8080", fails=0, latency=2.0)
        # 候选 2 个：0.5s（填缺口）和 1.5s（更快踢尾）
        self.pool.add_to_group("default-http", "http://10.0.0.1:8080")
        self.pool.add_to_group("default-http", "http://10.0.0.2:8080")
        spec = {
            "http://10.0.0.1:8080": {"success": True, "elapsed": 0.5},
            "http://10.0.0.2:8080": {"success": True, "elapsed": 1.5},
        }
        c = make_standby(self.pool, make_check_results(spec), fill_groups=("default-http",), multiplier=2, min_extra=0)
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=2))
        # 0.5s 入队填缺口（队列：0.5s, 2.0s）；1.5s < 队尾 2.0s -> 踢尾入新（队列：0.5s, 1.5s）
        self.assertEqual(added, 2)
        self.assertEqual(c.standby_proxies(), ["http://10.0.0.1:8080", "http://10.0.0.2:8080"])
        # 被踢出的 2.0s move trusted
        self.assertIn("http://10.0.0.0:8080", self.pool.get_group_proxies(TRUSTED_GROUP))

    def test_fill_slower_new_goes_to_trusted(self):
        """队列填满后后续更慢代理不替换，move trusted"""
        # 预填队列 1 个快代理 latency=0.5s（留 1 个缺口）
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.0:8080", fails=0, latency=0.5)
        # 候选 2 个：1.0s（填缺口）和 1.5s（更慢不替换）
        self.pool.add_to_group("default-http", "http://10.0.0.1:8080")
        self.pool.add_to_group("default-http", "http://10.0.0.2:8080")
        spec = {
            "http://10.0.0.1:8080": {"success": True, "elapsed": 1.0},
            "http://10.0.0.2:8080": {"success": True, "elapsed": 1.5},
        }
        c = make_standby(self.pool, make_check_results(spec), fill_groups=("default-http",), multiplier=2, min_extra=0)
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=2))
        # 1.0s 入队填缺口（队列：0.5s, 1.0s）；1.5s >= 队尾 1.0s -> 不替换，move trusted
        self.assertEqual(added, 1)
        self.assertEqual(c.standby_proxies(), ["http://10.0.0.0:8080", "http://10.0.0.1:8080"])
        self.assertIn("http://10.0.0.2:8080", self.pool.get_group_proxies(TRUSTED_GROUP))

    def test_fill_promotes_surplus_alive_to_trusted(self):
        """存活数超过缺口时，入队剩余的存活代理 move 进 trusted（探测成果不浪费）"""
        proxies = [f"http://10.0.0.{i}:8080" for i in range(4)]
        for p in proxies:
            self.pool.add_to_group("default-http", p)
        check = MagicMock()

        async def execute(proxies, url, **kw):
            results = {p: {"success": True, "status_code": 200, "elapsed": 0.1, "error": None} for p in proxies}
            _fire_on_result(proxies, results, kw)
            return results

        check.execute.side_effect = execute
        c = make_standby(self.pool, check, fill_groups=("default-http",), multiplier=2, min_extra=0)
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=4, capacity=2))
        # 缺口 2 × 倍率 2 = 4 候选全探测；前 2 个入备用队列
        self.assertEqual(added, 2)
        self.assertEqual(len(c.standby_proxies()), 2)
        # 后 2 个 move 进 trusted（不在原组、不在 standby）
        trusted = self.pool.get_group_proxies(TRUSTED_GROUP)
        self.assertEqual(len(trusted), 2)
        for p in trusted:
            self.assertNotIn(p, self.pool.get_group_proxies("default-http"))
            self.assertNotIn(p, c.standby_proxies())

    def test_fill_invokes_on_change_per_admission(self):
        """on_change 回调在每次入队/替换时调用（serve 用来实时 sync pproxy 上游）"""
        self.pool.add_to_group("default-http", "http://10.0.0.0:8080")
        self.pool.add_to_group("default-http", "http://10.0.0.1:8080")
        c = make_standby(self.pool, make_check({"http://10.0.0.0:8080", "http://10.0.0.1:8080"}),
                         fill_groups=("default-http",), multiplier=1, min_extra=0)
        calls = []
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5,
                            on_change=lambda: calls.append(1)))
        self.assertEqual(len(calls), 2)  # 两个都入队，on_change 调 2 次


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

    def test_heartbeat_updates_latency_and_reorders(self):
        """heartbeat 探测成功更新 latency，standby_proxies 按新 latency 重排"""
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:3128", fails=0, latency=0.1)
        # 探测后 1.2.3.4 变慢（0.5s），5.6.7.8 变快（0.2s）
        spec = {
            "http://1.2.3.4:8080": {"success": True, "elapsed": 0.5},
            "http://5.6.7.8:3128": {"success": True, "elapsed": 0.2},
        }
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        # 按 latency 升序：0.2 < 0.5
        self.assertEqual(c.standby_proxies(), ["http://5.6.7.8:3128", "http://1.2.3.4:8080"])

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
            results = _async_result({p: True for p in proxies})
            _fire_on_result(proxies, results, kw)
            return results

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
