"""LocalNetworkGate 单元测试：多目标直连 + 缓存 + 退避

async_getter 全程 mock，不依赖网络。
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock

from src.use_cases.network_gate import LocalNetworkGate


def make_async_getter(ok_urls):
    """async_getter mock：ok_urls 内的 URL 请求成功，其余抛异常

    成功返回 duck-typed response（只读 status_code，与 AsyncHttpGetter 协议一致）。
    """
    getter = MagicMock()

    async def get(url, *, timeout, **kw):
        if url in ok_urls:
            return MagicMock(status_code=204)
        raise ConnectionError(f"unreachable {url}")

    getter.get.side_effect = get
    return getter


class TestNetworkGate(unittest.TestCase):
    def test_any_target_ok_returns_true(self):
        """多目标任一通即认为网络正常"""
        getter = make_async_getter({"https://a.local"})
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local", "https://b.local", "https://c.local"],
            cache_ok=0, backoff_initial=0.1, backoff_max=1,
        )
        self.assertTrue(asyncio.run(gate.check(timeout=1)))

    def test_all_targets_fail_returns_false(self):
        """所有目标都不可达时返回 False"""
        getter = make_async_getter(set())
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local", "https://b.local"],
            cache_ok=0, backoff_initial=0.1, backoff_max=1,
        )
        self.assertFalse(asyncio.run(gate.check(timeout=1)))

    def test_empty_probe_urls_returns_false(self):
        """无探测目标时视为异常（无法判定网络可用）"""
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=make_async_getter(set()),
            probe_urls=[],
            cache_ok=0, backoff_initial=0.1, backoff_max=1,
        )
        self.assertFalse(asyncio.run(gate.check(timeout=1)))

    def test_cache_ok_reuses_result(self):
        """网络正常时 cache_ok 期内复用结果，不重复直连"""
        getter = make_async_getter({"https://a.local"})
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local"],
            cache_ok=10, backoff_initial=0.1, backoff_max=1,
        )
        asyncio.run(gate.check(timeout=1))
        asyncio.run(gate.check(timeout=1))
        asyncio.run(gate.check(timeout=1))
        # 缓存期内只直连一次（3 次 check 只触发 1 次探测）
        self.assertEqual(getter.get.call_count, 1)

    def test_backoff_doubles_on_failure(self):
        """网络异常时退避间隔倍增（上限 backoff_max）"""
        getter = make_async_getter(set())
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local"],
            cache_ok=0, backoff_initial=0.05, backoff_max=1,
        )
        asyncio.run(gate.check(timeout=1))  # 失败：backoff 0.05 -> 0.1
        self.assertAlmostEqual(gate._backoff, 0.1)
        time.sleep(0.11)
        asyncio.run(gate.check(timeout=1))  # 失败：backoff 0.1 -> 0.2
        self.assertAlmostEqual(gate._backoff, 0.2)

    def test_backoff_capped_at_max(self):
        """退避间隔不超过 backoff_max"""
        getter = make_async_getter(set())
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local"],
            cache_ok=0, backoff_initial=0.05, backoff_max=0.15,
        )
        asyncio.run(gate.check(timeout=1))  # 0.05 -> 0.1
        time.sleep(0.11)
        asyncio.run(gate.check(timeout=1))  # 0.1 -> 0.15 (capped)
        self.assertAlmostEqual(gate._backoff, 0.15)
        time.sleep(0.16)
        asyncio.run(gate.check(timeout=1))  # 0.15 -> 0.15 (capped)
        self.assertAlmostEqual(gate._backoff, 0.15)

    def test_backoff_reset_on_recovery(self):
        """网络恢复后退避重置为初始值"""
        getter = make_async_getter(set())
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local"],
            cache_ok=0, backoff_initial=0.05, backoff_max=1,
        )
        asyncio.run(gate.check(timeout=1))  # 失败
        self.assertFalse(gate.is_ok)
        # 网络恢复
        async def ok_get(url, **kw):
            return MagicMock(status_code=204)
        getter.get.side_effect = ok_get
        time.sleep(gate._backoff + 0.01)
        asyncio.run(gate.check(timeout=1))  # 恢复
        self.assertTrue(gate.is_ok)
        self.assertAlmostEqual(gate._backoff, 0.05)  # 重置为 initial

    def test_failure_uses_backoff_as_cache_ttl(self):
        """网络异常时按退避间隔缓存（期内复用 False，不重复直连）"""
        getter = make_async_getter(set())
        gate = LocalNetworkGate(
            http_getter=MagicMock(),
            async_getter=getter,
            probe_urls=["https://a.local"],
            cache_ok=0, backoff_initial=10, backoff_max=10,
        )
        asyncio.run(gate.check(timeout=1))  # 失败，backoff 10 -> 10 (capped)
        asyncio.run(gate.check(timeout=1))  # 缓存期内（backoff=10s），复用 False
        self.assertEqual(getter.get.call_count, 1)  # 只探测一次


if __name__ == "__main__":
    unittest.main()
