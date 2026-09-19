"""LocalNetworkGate 单元测试：链路判定（状态码/多目标/每次真实探测/并发共享）

async_getter 全程 mock，不依赖网络。
"""

import asyncio
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


def make_gate(getter, *, probe_urls=("https://probe.local",), probe_timeout=1):
    return LocalNetworkGate(
        async_getter=getter,
        probe_urls=probe_urls,
        probe_timeout=probe_timeout,
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

    def test_each_check_probes_freshly(self):
        """每次 check() 都真实探测，无结论缓存"""
        getter = FakeGetter()
        gate = make_gate(getter, probe_urls=("https://a.local", "https://b.local"))
        for _ in range(3):
            self.assertTrue(asyncio.run(gate.check()))
        self.assertEqual(getter.calls, 6)  # 3 次调用 × 2 个目标，每次全量探测

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


if __name__ == "__main__":
    unittest.main()
