"""LocalNetworkGate 单元测试：链路判定（状态码/缓存/退避/并发共享）+ 链路事件判定

async_getter 全程 mock，不依赖网络。
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock

from src.use_cases.network_gate import LocalNetworkGate


class FakeGetter:
    """async_getter mock：fail_urls 内的 URL 抛异常，其余返回指定状态码"""

    def __init__(self, *, fail_urls=(), status=204, delay=0.0):
        self.fail_urls = set(fail_urls)
        self.status = status
        self.delay = delay
        self.calls = 0

    async def get(self, url, *, timeout, verify=False):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.fail_urls:
            raise ConnectionError(f"unreachable {url}")
        return MagicMock(status_code=self.status)


def make_gate(getter, *, probe_urls=("https://probe.local",), probe_timeout=1,
              cache_ok=0, backoff_initial=0.05, backoff_max=1,
              link_event_fail_ratio=0.8, link_event_min_sample=4):
    return LocalNetworkGate(
        async_getter=getter,
        probe_urls=probe_urls,
        probe_timeout=probe_timeout,
        cache_ok=cache_ok,
        backoff_initial=backoff_initial,
        backoff_max=backoff_max,
        link_event_fail_ratio=link_event_fail_ratio,
        link_event_min_sample=link_event_min_sample,
    )


class TestLinkCheck(unittest.TestCase):
    def test_any_reachable_target_is_healthy(self):
        """多目标任一可达即认为链路可用"""
        getter = FakeGetter(fail_urls=("https://b.local",))
        gate = make_gate(getter, probe_urls=("https://a.local", "https://b.local"))
        self.assertTrue(asyncio.run(gate.check()))

    def test_all_targets_unreachable_is_unhealthy(self):
        getter = FakeGetter(fail_urls=("https://a.local", "https://b.local"))
        gate = make_gate(getter, probe_urls=("https://a.local", "https://b.local"))
        self.assertFalse(asyncio.run(gate.check()))

    def test_error_status_code_is_unreachable(self):
        """4xx/5xx（中间设备/门户错误页）不算链路可用"""
        self.assertFalse(asyncio.run(make_gate(FakeGetter(status=502)).check()))

    def test_healthy_result_reused_within_cache_window(self):
        getter = FakeGetter()
        gate = make_gate(getter, probe_urls=("https://a.local", "https://b.local"), cache_ok=10)
        for _ in range(3):
            self.assertTrue(asyncio.run(gate.check()))
        self.assertEqual(getter.calls, 2)  # 只探测首轮（2 个目标各一次）

    def test_unhealthy_result_reused_for_backoff(self):
        getter = FakeGetter(fail_urls=("https://probe.local",))
        gate = make_gate(getter, backoff_initial=10, backoff_max=10)
        self.assertFalse(asyncio.run(gate.check()))
        self.assertFalse(asyncio.run(gate.check()))
        self.assertEqual(getter.calls, 1)

    def test_backoff_grows_and_caps(self):
        """连续失败：复检间隔 ×2 递增并封顶（用探测次数观察）"""
        getter = FakeGetter(fail_urls=("https://probe.local",))
        gate = make_gate(getter, backoff_initial=0.05, backoff_max=0.2)
        self.assertFalse(asyncio.run(gate.check()))  # 探测 1：间隔 0.05 -> 0.1
        self.assertFalse(asyncio.run(gate.check()))  # 0.1 内复用结论，不探测
        self.assertEqual(getter.calls, 1)
        time.sleep(0.11)
        self.assertFalse(asyncio.run(gate.check()))  # 探测 2：间隔 0.1 -> 0.2
        self.assertEqual(getter.calls, 2)
        time.sleep(0.21)
        self.assertFalse(asyncio.run(gate.check()))  # 探测 3：间隔封顶 0.2
        self.assertEqual(getter.calls, 3)

    def test_recovery_resets_backoff(self):
        getter = FakeGetter(fail_urls=("https://probe.local",))
        gate = make_gate(getter, backoff_initial=0.05, backoff_max=1)
        self.assertFalse(asyncio.run(gate.check()))  # 失败：间隔 0.1
        time.sleep(0.11)
        getter.fail_urls.clear()
        self.assertTrue(asyncio.run(gate.check()))   # 恢复：间隔重置 0.05
        getter.fail_urls.add("https://probe.local")
        self.assertFalse(asyncio.run(gate.check()))  # 再失败：间隔 0.1（未重置会是 0.2 而命中缓存）
        time.sleep(0.11)
        self.assertFalse(asyncio.run(gate.check()))
        self.assertEqual(getter.calls, 4)

    def test_concurrent_checks_share_one_probe(self):
        """并发调用共享同一次探测（不被并发放大）"""
        getter = FakeGetter(delay=0.05)
        gate = make_gate(getter, probe_urls=("https://a.local", "https://b.local"))

        async def main():
            return await asyncio.gather(gate.check(), gate.check(), gate.check())

        self.assertEqual(asyncio.run(main()), [True, True, True])
        self.assertEqual(getter.calls, 2)

    def test_concurrent_checks_never_get_stale_result(self):
        """并发调用都拿到本次探测的结论，不会有人读到探测前的陈旧值"""
        getter = FakeGetter(fail_urls=("https://probe.local",), delay=0.05)
        gate = make_gate(getter)

        async def main():
            return await asyncio.gather(gate.check(), gate.check())

        self.assertEqual(asyncio.run(main()), [False, False])


class TestLinkEventAssessment(unittest.TestCase):
    def test_batch_over_threshold_is_link_event(self):
        """失败面超阈值判为链路事件，同时作废结论让下次 check 立即复检"""
        getter = FakeGetter()
        gate = make_gate(getter, cache_ok=100)
        self.assertTrue(asyncio.run(gate.check()))
        self.assertFalse(gate.assess_batch(4, 4))
        asyncio.run(gate.check())
        self.assertEqual(getter.calls, 2)  # 结论已作废 -> 立即复检

    def test_fail_ratio_at_threshold_is_link_event(self):
        self.assertFalse(make_gate(FakeGetter()).assess_batch(4, 5))  # 80% 达到阈值

    def test_small_sample_or_low_ratio_is_trustworthy(self):
        getter = FakeGetter()
        gate = make_gate(getter, cache_ok=100)
        self.assertTrue(asyncio.run(gate.check()))
        self.assertTrue(gate.assess_batch(3, 3))   # 样本不足
        self.assertTrue(gate.assess_batch(3, 10))  # 失败率 30%
        asyncio.run(gate.check())
        self.assertEqual(getter.calls, 1)          # 结论未作废，仍走缓存


if __name__ == "__main__":
    unittest.main()
