"""dns_probe 用例测试：入队即探测、在途去重、冷却/退避、限并发、跳过规则与结果交付

全部用假端口（resolver/geo/proxy_lookup/sink），不打网络也不碰 SQLite；时间相关用
补丁 time.time 精确控制（冷却与内存调度表清理都基于它）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import unittest
from unittest.mock import patch

from src.use_cases.dns_probe import DnsProbeQueue

_BIND = "1.1.1.1:8080"
_PROXY = "http://1.1.1.1:8080"
_US_GEO = {"country": "美国", "country_code": "US", "province": None, "city": None,
           "lat": 37.75, "lng": -97.82}
_UNSET = object()


class FakeResolver:
    """按脚本解析的假 DoH：host 未配置返回 1.2.3.4；值为 Exception 时抛出"""

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0

    async def resolve(self, host, *, proxy):
        self.calls.append((host, proxy))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)  # 让并发任务真正交错，暴露信号量是否生效
            result = self.results.get(host, ["1.2.3.4"])
            if isinstance(result, Exception):
                raise result
            return list(result)
        finally:
            self.inflight -= 1


class FakeGeo:
    """按脚本返回归属的假 IP 归属查询"""

    def __init__(self, geo=None):
        self.geo = dict(geo or {})
        self.calls = []

    async def resolve(self, ips, *, label):
        self.calls.append((tuple(ips), label))
        return {ip: self.geo[ip] for ip in ips if ip in self.geo}


class FakeSink:
    """结果接收方（模拟流量统计的 note_probe_result）"""

    def __init__(self):
        self.calls = []

    def __call__(self, bind, host, row):
        self.calls.append((bind, host, dict(row)))


def _make_queue(*, resolver=None, geo=_UNSET, proxy_lookup=_UNSET,
                concurrency=2, cache_ttl=100.0):
    """在事件循环内构造队列（asyncio 原语绑定当前循环），并接线假 sink"""
    resolver = resolver or FakeResolver()
    if geo is _UNSET:
        geo = FakeGeo({"1.2.3.4": _US_GEO})
    if proxy_lookup is _UNSET:
        proxy_lookup = lambda bind: _PROXY if bind == _BIND else None  # noqa: E731
    sink = FakeSink()
    queue = DnsProbeQueue(resolver=resolver, proxy_lookup=proxy_lookup, geo=geo,
                          concurrency=concurrency, cache_ttl=cache_ttl)
    queue.set_sink(sink)
    return queue, sink, resolver, geo


@contextlib.asynccontextmanager
async def _consumer(queue):
    """跑常驻消费者，退出时取消（模拟 serve 的任务生命周期）"""
    task = asyncio.create_task(queue.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _drain(queue, keys, timeout=1.0):
    """等到给定 pair 都不再在途（探测出终态）"""
    deadline = time.monotonic() + timeout
    while any(queue.is_pending(*k) for k in keys) and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0)  # 让最后一次 sink 调用落地


class TestEnqueueAndProbe(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_probes_via_proxy_and_delivers_row(self):
        queue, sink, resolver, geo = _make_queue()
        key = (_BIND, "example.com")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(resolver.calls, [("example.com", _PROXY)])
        self.assertEqual(geo.calls, [(("1.2.3.4",), "dns_probe")])
        self.assertEqual(len(sink.calls), 1)
        bind, host, row = sink.calls[0]
        self.assertEqual((bind, host), key)
        self.assertEqual(row["ip"], "1.2.3.4")
        self.assertEqual(row["country_code"], "US")
        self.assertEqual(row["lat"], 37.75)

    async def test_inflight_dedup_single_probe(self):
        queue, sink, resolver, _ = _make_queue()
        key = (_BIND, "example.com")
        async with _consumer(queue):
            queue.enqueue(*key)
            queue.enqueue(*key)  # 在途重复入队被丢弃
            await _drain(queue, [key])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(len(sink.calls), 1)

    async def test_ip_literal_skips_doh_direct_locate(self):
        geo = FakeGeo({"8.8.8.8": _US_GEO})
        queue, sink, resolver, _ = _make_queue(geo=geo)
        key = (_BIND, "8.8.8.8")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [(("8.8.8.8",), "dns_probe")])
        self.assertEqual(sink.calls[0][2]["country_code"], "US")

    async def test_empty_answer_delivers_negative_row(self):
        resolver = FakeResolver({"nx.example": []})
        queue, sink, _, geo = _make_queue(resolver=resolver)
        key = (_BIND, "nx.example")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(geo.calls, [])
        self.assertEqual(len(sink.calls), 1)
        row = sink.calls[0][2]
        self.assertIsNone(row.get("ip"))
        self.assertIsNone(row.get("lat"))

    async def test_geo_miss_delivers_ip_only_row(self):
        resolver = FakeResolver({"cdn.example": ["203.0.113.9"]})
        queue, sink, _, _ = _make_queue(resolver=resolver)
        key = (_BIND, "cdn.example")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        row = sink.calls[0][2]
        self.assertEqual(row["ip"], "203.0.113.9")
        self.assertIsNone(row.get("country_code"))

    async def test_geo_disabled_delivers_ip_only_row(self):
        queue, sink, _, _ = _make_queue(geo=None)
        key = (_BIND, "example.com")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        row = sink.calls[0][2]
        self.assertEqual(row, {"ip": "1.2.3.4"})

    async def test_geo_first_hit_within_two_addresses(self):
        resolver = FakeResolver({"multi.example": ["1.1.1.1", "2.2.2.2", "3.3.3.3"]})
        geo = FakeGeo({"2.2.2.2": _US_GEO})
        queue, sink, _, geo = _make_queue(resolver=resolver, geo=geo)
        key = (_BIND, "multi.example")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(len(geo.calls), 2)  # 只定位前两个 A 记录
        self.assertEqual(sink.calls[0][2]["ip"], "2.2.2.2")


class TestSkipRules(unittest.IsolatedAsyncioTestCase):
    async def test_private_literals_and_intranet_names_never_enqueued(self):
        queue, sink, resolver, geo = _make_queue()
        keys = [(_BIND, h) for h in ("192.168.1.1", "10.0.0.5", "::1",
                                     "intranet", "printer.local")]
        async with _consumer(queue):
            for key in keys:
                queue.enqueue(*key)
            await asyncio.sleep(0.01)
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [])
        self.assertEqual(sink.calls, [])
        self.assertFalse(any(queue.is_pending(*k) for k in keys))

    async def test_proxy_off_queue_skips_without_backoff(self):
        queue, sink, resolver, _ = _make_queue(proxy_lookup=lambda bind: None)
        key = (_BIND, "example.com")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(sink.calls, [])
        self.assertEqual(queue._schedule, {}, "代理离队不记退避")

    async def test_invalid_hostname_skips_without_backoff(self):
        resolver = FakeResolver({"bad-.example": ValueError("IDNA 编码失败")})
        queue, sink, _, _ = _make_queue(resolver=resolver)
        key = (_BIND, "bad-.example")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(sink.calls, [])
        self.assertEqual(queue._schedule, {}, "非法主机名跳过不记退避")


class TestSchedule(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_blocks_until_cache_ttl(self):
        queue, sink, resolver, _ = _make_queue(cache_ttl=100.0)
        key = (_BIND, "example.com")
        with patch("src.use_cases.dns_probe.time.time", return_value=1000.0):
            async with _consumer(queue):
                queue.enqueue(*key)
                await _drain(queue, [key])
                self.assertEqual(len(resolver.calls), 1)
                queue.enqueue(*key)  # 冷却期内
                await asyncio.sleep(0.01)
                self.assertEqual(len(resolver.calls), 1)
        with patch("src.use_cases.dns_probe.time.time", return_value=1101.0):
            async with _consumer(queue):
                queue.enqueue(*key)
                await _drain(queue, [key])
        self.assertEqual(len(resolver.calls), 2)
        self.assertEqual(len(sink.calls), 2)

    async def test_failure_backoff_then_recovery(self):
        base = 1000.0
        resolver = FakeResolver({"bad.example": RuntimeError("dns down")})
        queue, sink, resolver, _ = _make_queue(resolver=resolver, cache_ttl=100.0)
        key = (_BIND, "bad.example")
        with patch("src.use_cases.dns_probe.time.time", return_value=base):
            async with _consumer(queue):
                queue.enqueue(*key)
                await _drain(queue, [key])
                self.assertEqual(len(resolver.calls), 1)
                self.assertEqual(sink.calls, [], "失败不交付结果")
                queue.enqueue(*key)  # 退避期内不再尝试
                await asyncio.sleep(0.01)
                self.assertEqual(len(resolver.calls), 1)
        with patch("src.use_cases.dns_probe.time.time", return_value=base + 61):
            async with _consumer(queue):
                queue.enqueue(*key)
                await _drain(queue, [key])
                self.assertEqual(len(resolver.calls), 2, "退避到期后重试")
        resolver.results["bad.example"] = ["1.2.3.4"]
        with patch("src.use_cases.dns_probe.time.time", return_value=base + 61 + 121):
            async with _consumer(queue):
                queue.enqueue(*key)
                await _drain(queue, [key])
        self.assertEqual(sink.calls[0][2]["ip"], "1.2.3.4")
        # 成功后冷却档位复位（退避秒数回到初始值）
        self.assertEqual(queue._schedule[key][1], 60.0)

    async def test_backoff_exponential_capped_at_15min(self):
        queue, *_ = _make_queue()
        key = (_BIND, "example.com")
        waits = []
        for i in range(6):
            now = 1000.0 + i
            queue._note_failure(key, now)
            waits.append(round(queue._schedule[key][0] - now))
        self.assertEqual(waits, [60, 120, 240, 480, 900, 900])

    async def test_unexpected_exception_is_backoff_not_crash(self):
        resolver = FakeResolver({"boom.example": KeyError("unexpected")})
        queue, sink, _, _ = _make_queue(resolver=resolver)
        key = (_BIND, "boom.example")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
            # 消费者存活：仍能处理后续 pair
            queue.enqueue(_BIND, "ok.example")
            await _drain(queue, [(_BIND, "ok.example")])
        self.assertIn(key, queue._schedule)
        self.assertEqual([c[1] for c in sink.calls], ["ok.example"])

    async def test_prune_clears_stale_schedule_entries(self):
        queue, *_ = _make_queue(cache_ttl=100.0)
        queue._schedule[(_BIND, "old.example")] = (900.0, 60.0)
        queue._schedule[(_BIND, "new.example")] = (1500.0, 60.0)
        queue.prune(now=1100.0)  # 阈值 1100-100=1000：早于阈值的丢弃
        self.assertNotIn((_BIND, "old.example"), queue._schedule)
        self.assertIn((_BIND, "new.example"), queue._schedule)


class TestConcurrencyAndPending(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_caps_inflight_probes(self):
        pairs = [(_BIND, f"h{i}.example") for i in range(6)]
        queue, sink, resolver, _ = _make_queue(concurrency=2)
        async with _consumer(queue):
            for key in pairs:
                queue.enqueue(*key)
            await _drain(queue, pairs)
        self.assertEqual(len(resolver.calls), 6)
        self.assertLessEqual(resolver.max_inflight, 2)
        self.assertEqual(len(sink.calls), 6)

    async def test_is_pending_lifecycle(self):
        queue, sink, _, _ = _make_queue()
        key = (_BIND, "example.com")
        self.assertFalse(queue.is_pending(*key))
        async with _consumer(queue):
            queue.enqueue(*key)
            self.assertTrue(queue.is_pending(*key), "入队即在途")
            await _drain(queue, [key])
            self.assertFalse(queue.is_pending(*key), "出终态即不在途")
        self.assertEqual(len(sink.calls), 1)

    async def test_no_sink_drops_result_without_raising(self):
        queue, _, resolver, _ = _make_queue()
        queue._sink = None  # 未接线：结果丢弃，探测任务不崩
        key = (_BIND, "example.com")
        async with _consumer(queue):
            queue.enqueue(*key)
            await _drain(queue, [key])
        self.assertEqual(len(resolver.calls), 1)
        self.assertFalse(queue.is_pending(*key))


if __name__ == "__main__":
    unittest.main()