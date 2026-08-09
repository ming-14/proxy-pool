"""StandbyQueueUseCase 单元测试：fill 流式补入/排序淘汰 / heartbeat 计次淘汰 / 退避补测

探测（check_proxies）全程 mock，不依赖网络。mock 需调 on_result 模拟流式回调。
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock

from src.domain.proxy_pool import ProxyPool, LOCKED_GROUP
from src.framework.config import (
    PROXY_GROUPS, MAX_CONSECUTIVE_FAILS, TRUSTED_MAX_CONSECUTIVE_FAILS, SOURCE_PAUSE_TTL,
    TRUSTED_GROUP, BLACKLIST_GROUP, STANDBY_GROUP,
)
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.standby import StandbyQueueUseCase


def make_pool():
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
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


def make_gate(ok: bool = True):
    """LocalNetworkGate mock：check 总是返回 ok（不依赖网络）"""
    gate = MagicMock()
    gate.is_ok = ok

    async def check(timeout):
        return ok

    gate.check.side_effect = check
    return gate


def make_standby(pool, check, fill_groups=("default-http",), multiplier=2, min_extra=1,
                 metrics_window=1800, gate=None):
    return StandbyQueueUseCase(
        pool=pool,
        record=RecordProxyUseCase(pool=pool, standby_group=STANDBY_GROUP),
        check_proxies=check,
        network_gate=gate or make_gate(True),
        standby_group=STANDBY_GROUP,
        fill_groups=fill_groups,
        fill_probe_multiplier=multiplier,
        fill_probe_min_extra=min_extra,
        metrics_window=metrics_window,
    )


class TestStandbyFill(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()

    def test_fill_moves_alive_proxies_blacklists_dead(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        # 预设 consecutive_fails=1，探测失败后 consecutive_fails=2=MAX_CONSECUTIVE_FAILS -> 拉黑（fill 用 max_consecutive_fails 机制，非单次拉黑）
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128", consecutive_fails=1)
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 1)
        self.assertEqual(c.standby_proxies(), ["http://1.2.3.4:8080"])
        self.assertTrue(self.pool.is_blacklisted("http://5.6.7.8:3128"))
        # 入队连续失败计数清零
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 0)

    def test_fill_failure_records_consecutive_fails_not_blacklist(self):
        """探测失败只 record_fail，未达 max_consecutive_fails 不拉黑，留原组下次可再被选"""
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 1)
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", self.pool.get_group_proxies("default-http"))

    def test_fill_skips_existing_standby_members(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)
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
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)
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
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.0:8080", consecutive_fails=0, latency=2.0)
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
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.0:8080", consecutive_fails=0, latency=0.5)
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
        # 缺口 2 × 倍率 2 = 4 候选全探测；前 2 个入代理服务队列
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
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)

    def test_heartbeat_success_resets_consecutive_fails_keeps_in_queue(self):
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"] = 1
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, ["http://1.2.3.4:8080"])
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 0)
        self.assertEqual(c.pending_retries(), [])

    def test_heartbeat_updates_latency_and_reorders(self):
        """heartbeat 探测成功更新 latency，standby_proxies 按新 latency 重排"""
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:3128", consecutive_fails=0, latency=0.1)
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
        # 一次失败：计 1 次，未达阈值（MAX_CONSECUTIVE_FAILS=2），进入退避补测，仍在队列
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 1)
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


class TestStandbyMetrics(unittest.TestCase):
    """heartbeat 采样历史：每轮采一次，按时间窗口过期清理"""

    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0, latency=0.1)

    def test_heartbeat_samples_metrics(self):
        """heartbeat 后 history 含一个采样点，指标值正确"""
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        hist = c.history()
        self.assertEqual(len(hist), 1)
        s = hist[0]
        self.assertEqual(s.avg, 0.1)
        self.assertEqual(s.min, 0.1)
        self.assertEqual(s.max, 0.1)
        self.assertEqual(s.health_rate, 100)
        self.assertEqual(s.queue_length, 1)

    def test_heartbeat_skipped_no_sample(self):
        """空队列/全在补测时 heartbeat 不采样（无可观测指标）"""
        self.pool.move_proxy("http://1.2.3.4:8080", self.pool.blacklist_group)
        c = make_standby(self.pool, MagicMock())
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(c.history(), [])

    def test_metrics_expire_by_window(self):
        """超出 metrics_window 的旧采样点被清理，窗口内保留"""
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}), metrics_window=0.05)
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        time.sleep(0.06)
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        hist = c.history()
        self.assertEqual(len(hist), 1)  # 第一次的已过期，只留第二次

    def test_metrics_multiple_proxies(self):
        """多代理采样：avg/min/max/health_rate 聚合正确"""
        self.pool.add_to_group(STANDBY_GROUP, "http://5.6.7.8:3128", consecutive_fails=1, latency=0.3)
        spec = {
            "http://1.2.3.4:8080": {"success": True, "elapsed": 0.1},
            "http://5.6.7.8:3128": {"success": True, "elapsed": 0.3},
        }
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        s = c.history()[0]
        self.assertAlmostEqual(s.avg, 0.2)
        self.assertEqual(s.min, 0.1)
        self.assertEqual(s.max, 0.3)
        self.assertEqual(s.queue_length, 2)
        # 5.6.7.8 探测前 consecutive_fails=1，探测成功清零 -> 两个都健康
        self.assertEqual(s.health_rate, 100)


class TestStandbyRetryProbe(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)

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
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 0)
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertEqual(c.pending_retries(), [])

    def test_retry_failure_blacklists_at_threshold(self):
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        # 心跳 1 次 + 补测 1 次 = 2 = MAX_CONSECUTIVE_FAILS -> 拉黑移出
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertNotIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertEqual(c.pending_retries(), [])

    def test_retry_stops_when_proxy_leaves_queue(self):
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.pool.move_proxy("http://1.2.3.4:8080", "default-http", consecutive_fails=0)
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        self.assertEqual(c.pending_retries(), [])
        self.assertNotIn("http://1.2.3.4:8080", c.standby_proxies())


class TestStandbyPinned(unittest.TestCase):
    """固定代理（pin/unpin）队列保护测试"""

    def setUp(self):
        self.pool = make_pool()

    def test_pinned_sort_first(self):
        """固定代理排最前（按 pinned_at 升序），非固定按 latency 升序排后"""
        c = make_standby(self.pool, MagicMock())
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.1:8080", consecutive_fails=0, latency=0.1)
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.2:8080", consecutive_fails=0, latency=0.2)
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.3:8080", consecutive_fails=0, latency=0.3)
        self.pool.pin("http://10.0.0.3:8080")  # 最慢但固定，应排第一
        self.assertEqual(c.standby_proxies(), [
            "http://10.0.0.3:8080",
            "http://10.0.0.1:8080",
            "http://10.0.0.2:8080",
        ])

    def test_pinned_order_by_pinned_at(self):
        """多个固定代理按 pinned_at 升序（先固定的排前）"""
        c = make_standby(self.pool, MagicMock())
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.1:8080", consecutive_fails=0, latency=0.1)
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.2:8080", consecutive_fails=0, latency=0.2)
        self.pool.pin("http://10.0.0.2:8080")
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://10.0.0.2:8080"]["pinned_at"] = 100
        self.pool.pin("http://10.0.0.1:8080")
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://10.0.0.1:8080"]["pinned_at"] = 200
        self.assertEqual(c.standby_proxies(), [
            "http://10.0.0.2:8080",
            "http://10.0.0.1:8080",
        ])

    def test_pinned_beyond_capacity_ignored(self):
        """超出 capacity 的固定代理忽略（保留在组内但不参与队列）"""
        c = make_standby(self.pool, MagicMock())
        c.set_capacity(2)
        for i in range(3):
            self.pool.add_to_group(STANDBY_GROUP, f"http://10.0.0.{i}:8080", consecutive_fails=0, latency=0.1 * i)
            self.pool.pin(f"http://10.0.0.{i}:8080")
        result = c.standby_proxies()
        self.assertEqual(len(result), 2)
        # 第 3 个仍在组内但被忽略
        self.assertIn("http://10.0.0.2:8080", self.pool.get_group_proxies(STANDBY_GROUP))

    def test_fill_does_not_kick_pinned(self):
        """fill 填满队列后后续更快候选踢非固定队尾，不踢固定"""
        # 队列：1 个固定（慢 latency=3.0），留 1 个缺口
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.1:8080", consecutive_fails=0, latency=3.0)
        self.pool.pin("http://10.0.0.1:8080")
        # 候选 2 个：0.5s 填缺口，0.4s 更快踢非固定队尾
        self.pool.add_to_group("default-http", "http://10.0.0.8:8080")
        self.pool.add_to_group("default-http", "http://10.0.0.9:8080")
        spec = {
            "http://10.0.0.8:8080": {"success": True, "elapsed": 0.5},
            "http://10.0.0.9:8080": {"success": True, "elapsed": 0.4},
        }
        c = make_standby(self.pool, make_check_results(spec), fill_groups=("default-http",), multiplier=2, min_extra=0)
        c.set_capacity(2)
        asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=2))
        # 固定代理仍在队列
        self.assertIn("http://10.0.0.1:8080", c.standby_proxies())
        # 0.5s 填缺口入队后被 0.4s 踢到 trusted，0.4s 入队
        self.assertIn("http://10.0.0.9:8080", c.standby_proxies())
        self.assertIn("http://10.0.0.8:8080", self.pool.get_group_proxies(TRUSTED_GROUP))

    def test_fill_skips_ignored_pinned_as_candidate(self):
        """被 capacity 忽略的固定代理不被 fill 当候选重新探测"""
        c = make_standby(self.pool, make_check_results({}), fill_groups=("default-http",), multiplier=1, min_extra=0)
        c.set_capacity(1)
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.1:8080", consecutive_fails=0, latency=0.1)
        self.pool.pin("http://10.0.0.1:8080")
        self.pool.add_to_group(STANDBY_GROUP, "http://10.0.0.2:8080", consecutive_fails=0, latency=0.2)
        self.pool.pin("http://10.0.0.2:8080")
        self.assertEqual(len(c.standby_proxies()), 1)
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=1, capacity=1))
        self.assertEqual(added, 0)

    def test_heartbeat_pinned_failure_only_counts(self):
        """固定代理心跳失败只计数，不拉黑、不补测、不移出"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.pin("http://1.2.3.4:8080")
        c = make_standby(self.pool, make_check(set()))
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, [])
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 1)
        self.assertEqual(c.pending_retries(), [])
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())

    def test_heartbeat_pinned_failure_repeated_never_blacklisted(self):
        """固定代理反复心跳失败达阈值也不拉黑不移出"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=0)
        self.pool.pin("http://1.2.3.4:8080")
        c = make_standby(self.pool, make_check(set()))
        for _ in range(5):
            asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())
        self.assertEqual(c.pending_retries(), [])


class TestStandbyNetworkGate(unittest.TestCase):
    """本地网络门控：异常时跳过 heartbeat/fill/retry_probe，不计数/不拉黑/不补测"""

    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)

    def test_heartbeat_skipped_when_network_down(self):
        """网络异常时跳过心跳：不探测、不计数、不拉黑、不进补测"""
        check = MagicMock()
        c = make_standby(self.pool, check, gate=make_gate(False))
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, [])
        check.execute.assert_not_called()  # 未发起探测
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 0)
        self.assertEqual(c.pending_retries(), [])
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", c.standby_proxies())

    def test_heartbeat_network_down_preserves_existing_fail_count(self):
        """网络异常时不清零已有失败计数（代理可能真有问题，网络恢复后继续累计）"""
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"] = 1
        check = MagicMock()
        c = make_standby(self.pool, check, gate=make_gate(False))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        # 计数保持 1（未探测、未清零、未增加）
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 1)

    def test_fill_skipped_when_network_down(self):
        """网络异常时跳过填充：不探测、返回 0、候选代理失败计数不变"""
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        check = MagicMock()
        c = make_standby(self.pool, check, gate=make_gate(False))
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 0)
        check.execute.assert_not_called()
        # 候选代理失败计数未被污染
        self.assertEqual(self.pool.get_consecutive_fail_count("http://5.6.7.8:3128"), 0)

    def test_retry_probe_network_down_no_count_no_blacklist(self):
        """网络异常时补测不计数、不拉黑，代理保留在队列

        retry_probe 在 gate 异常时重置退避到初始值快速轮询 gate 缓存，
        不调 check_proxies、不 record_fail。用 wait_for 超时取出（gate 一直异常，
        循环不退出），验证超时期间代理状态不变。
        """
        check = MagicMock()
        gate = make_gate(False)
        c = make_standby(self.pool, check, gate=gate)
        c._retrying.add("http://1.2.3.4:8080")  # 模拟 heartbeat 已放入补测
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(
                c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3),
                timeout=2.5,
            ))
        check.execute.assert_not_called()  # 网络异常期间未探测
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 0)
        self.assertFalse(self.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("http://1.2.3.4:8080", self.pool.get_group_proxies(STANDBY_GROUP))


class TestStandbyLock(unittest.TestCase):
    """锁定单一上游（lock/unlock）：lock 后 upstream_proxies 只返回锁定代理，standby_proxies 仍返回完整队列，fill/heartbeat 跳过"""

    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        self.pool.move_proxy("http://5.6.7.8:3128", STANDBY_GROUP, consecutive_fails=0)

    def test_lock_returns_only_locked_proxy(self):
        """lock 后 upstream_proxies 只返回锁定代理，standby_proxies 返回剩余队列"""
        c = make_standby(self.pool, MagicMock())
        c.lock("http://1.2.3.4:8080")
        self.assertTrue(c.is_locked())
        self.assertEqual(c.upstream_proxies(), ["http://1.2.3.4:8080"])
        self.assertEqual(c.standby_proxies(), ["http://5.6.7.8:3128"])

    def test_unlock_restores_queue(self):
        """unlock 后代理 move 回队列，恢复原 standby_proxies"""
        c = make_standby(self.pool, MagicMock())
        c.lock("http://1.2.3.4:8080")
        self.assertEqual(c.upstream_proxies(), ["http://1.2.3.4:8080"])
        c.unlock()
        self.assertFalse(c.is_locked())
        self.assertEqual(set(c.standby_proxies()), {"http://1.2.3.4:8080", "http://5.6.7.8:3128"})

    def test_fill_skipped_when_locked(self):
        """lock 后 fill 跳过（不探测，返回 0）"""
        check = MagicMock()
        c = make_standby(self.pool, check)
        c.lock("http://1.2.3.4:8080")
        added = asyncio.run(c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5))
        self.assertEqual(added, 0)
        check.execute.assert_not_called()

    def test_heartbeat_locked_probes_locked_proxy(self):
        """lock 后 heartbeat 仍探测锁定代理测速，成功更新 latency 并采样到趋势图"""
        c = make_standby(self.pool, make_check({"http://1.2.3.4:8080"}))
        c.lock("http://1.2.3.4:8080")
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, ["http://1.2.3.4:8080"])
        # latency 已更新（测速数据供趋势图）
        self.assertAlmostEqual(self.pool.get_latency("http://1.2.3.4:8080"), 0.1)
        # 采样加到 history（趋势图数据点）
        hist = c.history()
        self.assertEqual(len(hist), 1)
        self.assertTrue(hist[0].locked)  # 锁定期间采集
        self.assertAlmostEqual(hist[0].avg, 0.1)
        self.assertEqual(hist[0].queue_length, 1)
        self.assertEqual(hist[0].health_rate, 100)

    def test_heartbeat_locked_fail_only_counts(self):
        """lock 后心跳失败只计数，不拉黑不移出不补测（锁定语义：无论成败都用它）"""
        c = make_standby(self.pool, make_check(set()))  # 全失败
        c.lock("http://1.2.3.4:8080")
        alive = asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(alive, [])
        # 失败计数 +1 但代理仍在 LOCKED_GROUP（未拉黑未移出）
        self.assertEqual(self.pool.get_consecutive_fail_count("http://1.2.3.4:8080"), 1)
        self.assertIn("http://1.2.3.4:8080", self.pool.get_group_proxies(LOCKED_GROUP))
        # 未进入退避补测（锁定代理不补测）
        self.assertEqual(c.pending_retries(), [])

    def test_lock_preserves_other_proxies_in_queue(self):
        """lock 后其他代理保留在队列组（不动），unlock 后恢复"""
        c = make_standby(self.pool, MagicMock())
        c.lock("http://1.2.3.4:8080")
        # 5.6.7.8 仍在 standby 组
        self.assertIn("http://5.6.7.8:3128", self.pool.get_group_proxies(STANDBY_GROUP))
        # 仍在 standby_proxies（完整队列），但不在 upstream_proxies（锁定时只返回锁定代理）
        self.assertIn("http://5.6.7.8:3128", c.standby_proxies())
        self.assertNotIn("http://5.6.7.8:3128", c.upstream_proxies())


class TestPerProxyLatencyHistory(unittest.TestCase):
    """单代理延时历史采样测试"""

    def setUp(self):
        self.pool = make_pool()
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.move_proxy("http://1.2.3.4:8080", STANDBY_GROUP, consecutive_fails=0)

    def test_heartbeat_success_records_latency(self):
        """heartbeat 成功记一笔延时历史，key 为 bind（不含 scheme）"""
        spec = {"http://1.2.3.4:8080": {"success": True, "elapsed": 0.5}}
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        h = c.per_proxy_latency_history("1.2.3.4:8080")
        self.assertEqual(len(h), 1)
        self.assertAlmostEqual(h[0].latency, 0.5)

    def test_key_is_bind_not_full_proxy(self):
        """key 是 bind（不含 scheme），与 per-proxy 流量统一"""
        spec = {"http://1.2.3.4:8080": {"success": True, "elapsed": 0.3}}
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        # bind 格式有数据，完整 proxy 格式无数据
        self.assertEqual(len(c.per_proxy_latency_history("1.2.3.4:8080")), 1)
        self.assertEqual(len(c.per_proxy_latency_history("http://1.2.3.4:8080")), 0)

    def test_heartbeat_failure_not_recorded(self):
        """heartbeat 失败不记延时历史"""
        c = make_standby(self.pool, make_check(set()))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        self.assertEqual(c.per_proxy_latency_history("1.2.3.4:8080"), [])

    def test_multiple_heartbeats_accumulate(self):
        """多轮 heartbeat 各记一笔"""
        spec = {"http://1.2.3.4:8080": {"success": True, "elapsed": 0.4}}
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        h = c.per_proxy_latency_history("1.2.3.4:8080")
        self.assertEqual(len(h), 2)

    def test_retry_probe_success_records_latency(self):
        """retry_probe 补测成功也记一笔延时历史"""
        self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"] = 1
        spec = {"http://1.2.3.4:8080": {"success": True, "elapsed": 0.6}}
        c = make_standby(self.pool, make_check_results(spec))
        asyncio.run(c.retry_probe("http://1.2.3.4:8080", "http://probe.local", timeout=3))
        h = c.per_proxy_latency_history("1.2.3.4:8080")
        self.assertEqual(len(h), 1)
        self.assertAlmostEqual(h[0].latency, 0.6)

    def test_window_cleanup(self):
        """超出 metrics_window 的采样点自动清理"""
        spec = {"http://1.2.3.4:8080": {"success": True, "elapsed": 0.4}}
        c = make_standby(self.pool, make_check_results(spec), metrics_window=0.1)
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        time.sleep(0.15)
        asyncio.run(c.heartbeat("http://probe.local", timeout=3, concurrency=2))
        h = c.per_proxy_latency_history("1.2.3.4:8080")
        self.assertEqual(len(h), 1)

    def test_unknown_bind_returns_empty(self):
        """未探测过的 bind 返回空列表"""
        c = make_standby(self.pool, MagicMock())
        self.assertEqual(c.per_proxy_latency_history("9.9.9.9:9999"), [])


if __name__ == "__main__":
    unittest.main()
