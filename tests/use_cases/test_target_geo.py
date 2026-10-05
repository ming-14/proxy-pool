"""target_geo 用例测试：激活窗口、缓存 TTL、失败退避、限并发、跳过规则与聚合

全部用假端口（server/store/resolver/geo），不打网络也不碰 SQLite；时间相关用
补丁 time.time 精确控制（退避与清理节流都基于它）。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from src.use_cases.target_geo import TargetGeoUseCase

_BIND = "1.1.1.1:8080"
_PROXY = "http://1.1.1.1:8080"
_US_GEO = {"country": "美国", "country_code": "US", "province": None, "city": None,
           "lat": 37.75, "lng": -97.82}
_US_ROW = {"ip": "1.2.3.4", "country": "美国", "country_code": "US",
           "province": None, "city": "Ashburn", "lat": 39.0, "lng": -77.5}


class FakeServer:
    """活跃 pair 源（只实现 active_pairs 契约）"""

    def __init__(self, pairs):
        self.pairs = list(pairs)
        self.calls = []

    def active_pairs(self, since_ts):
        self.calls.append(since_ts)
        return [p for p in self.pairs if p["last_seen"] >= since_ts]


class FakeStore:
    """内存版目标归属缓存（记录写入/清理调用）"""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.saved = []
        self.pruned = []
        self.gets = []          # 每次批量读的 key 列表（验 build_flows 是否读缓存）
        self.get_error = None
        self.save_error = None
        self.prune_error = None

    def get_target_geo_batch(self, pairs):
        if self.get_error:
            raise self.get_error
        pairs = list(pairs)
        self.gets.append(pairs)
        return {k: self.rows[k] for k in pairs if k in self.rows}

    def save_target_geo_batch(self, rows):
        if self.save_error:
            raise self.save_error
        self.saved.append(dict(rows))
        for key, row in rows.items():
            # 与真实 store 同语义：缺失字段按 NULL 落库
            self.rows[key] = {
                "ip": row.get("ip"), "country": row.get("country"),
                "country_code": row.get("country_code"), "province": row.get("province"),
                "city": row.get("city"), "lat": row.get("lat"), "lng": row.get("lng"),
                "cached_at": time.time(),
            }

    def prune_target_geo(self, before_ts):
        if self.prune_error:
            raise self.prune_error
        self.pruned.append(before_ts)
        return 0


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


def _pair(host, *, bind=_BIND, last_seen=None, rate=0.0):
    return {"bind": bind, "host": host, "up": 0, "down": 0,
            "up_rate": rate, "down_rate": rate,
            "up_inst": rate, "down_inst": rate,
            "last_seen": time.time() if last_seen is None else last_seen}


def _use_case(*, pairs, rows=None, resolver=None, geo=None, proxies=None,
              concurrency=2, cache_ttl=100.0, activation_window=10.0,
              probe_window=100.0, prune_after=1000.0):
    server = FakeServer(pairs)
    store = FakeStore(rows)
    resolver = resolver or FakeResolver()
    geo = geo or FakeGeo({"1.2.3.4": _US_GEO})
    proxies = {_BIND: _PROXY} if proxies is None else proxies
    uc = TargetGeoUseCase(
        traffic=server, store=store, resolver=resolver, geo=geo,
        proxy_lookup=proxies.get, probe_concurrency=concurrency,
        cache_ttl=cache_ttl, activation_window=activation_window,
        probe_window=probe_window, prune_after=prune_after,
    )
    return uc, server, store, resolver, geo


class TestActivation(unittest.TestCase):
    def test_inactive_returns_zero_and_no_probe(self):
        uc, _, _, resolver, _ = _use_case(pairs=[_pair("example.com")])
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])

    def test_touch_activates_until_window_expires(self):
        uc, *_ = _use_case(pairs=[], activation_window=10.0)
        with patch("src.use_cases.target_geo.time.time", return_value=1000.0):
            uc.touch()
        with patch("src.use_cases.target_geo.time.time", return_value=1005.0):
            self.assertTrue(uc.is_active())
        with patch("src.use_cases.target_geo.time.time", return_value=1010.0):
            self.assertTrue(uc.is_active())   # 窗口边界内含
        with patch("src.use_cases.target_geo.time.time", return_value=1010.1):
            self.assertFalse(uc.is_active())


class TestProbeCycle(unittest.TestCase):
    def test_happy_path_probes_via_proxy_and_writes_row(self):
        uc, _, store, resolver, geo = _use_case(pairs=[_pair("example.com")])
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(resolver.calls, [("example.com", _PROXY)])
        self.assertEqual(geo.calls, [(("1.2.3.4",), "target")])
        row = store.rows[(_BIND, "example.com")]
        self.assertEqual(row["ip"], "1.2.3.4")
        self.assertEqual(row["country_code"], "US")
        self.assertEqual(row["lat"], 37.75)

    def test_fresh_cache_skips_probe(self):
        now = time.time()
        rows = {(_BIND, "example.com"): {"ip": "1.2.3.4", "lat": 1.0, "lng": 2.0,
                                         "cached_at": now - 5}}
        uc, _, _, resolver, geo = _use_case(pairs=[_pair("example.com")], rows=rows)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [])

    def test_expired_cache_reprobes(self):
        now = time.time()
        rows = {(_BIND, "example.com"): {"ip": "1.2.3.4", "lat": 1.0, "lng": 2.0,
                                         "cached_at": now - 101}}
        uc, _, store, resolver, _ = _use_case(pairs=[_pair("example.com")], rows=rows,
                                              cache_ttl=100.0)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(store.rows[(_BIND, "example.com")]["ip"], "1.2.3.4")

    def test_negative_row_blocks_reprobe_within_ttl(self):
        """已探测无结果的行同样受 TTL 保护（不因无坐标被反复重探）"""
        rows = {(_BIND, "nx.example"): {"ip": None, "lat": None, "lng": None,
                                        "cached_at": time.time() - 5}}
        uc, _, _, resolver, geo = _use_case(pairs=[_pair("nx.example")], rows=rows)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [])

    def test_failure_backoff_then_recovery_resets(self):
        base = time.time()
        resolver = FakeResolver({"bad.example": RuntimeError("dns down")})
        uc, _, store, resolver, _ = _use_case(
            pairs=[_pair("bad.example", last_seen=base)], resolver=resolver,
            cache_ttl=100.0, activation_window=1000.0, probe_window=1000.0)
        with patch("src.use_cases.target_geo.time.time", return_value=base):
            uc.touch()
            self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
            self.assertEqual(len(resolver.calls), 1)
            # 退避期内不再尝试
            self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
            self.assertEqual(len(resolver.calls), 1)
        with patch("src.use_cases.target_geo.time.time", return_value=base + 61):
            self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
            self.assertEqual(len(resolver.calls), 2)
            self.assertEqual(store.saved, [])
        resolver.results["bad.example"] = ["1.2.3.4"]
        with patch("src.use_cases.target_geo.time.time", return_value=base + 61 + 121):
            self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(uc._backoff, {}, "成功后必须清零退避")
        self.assertEqual(store.rows[(_BIND, "bad.example")]["ip"], "1.2.3.4")

    def test_backoff_exponential_capped_at_15min(self):
        uc, *_ = _use_case(pairs=[])
        key = (_BIND, "example.com")
        waits = []
        for i in range(6):
            now = 1000.0 + i
            uc._note_failure(key, now)
            waits.append(round(uc._backoff[key][0] - now))
        self.assertEqual(waits, [60, 120, 240, 480, 900, 900])

    def test_concurrency_semaphore_and_batch_cap(self):
        pairs = [_pair(f"h{i}.example") for i in range(10)]
        uc, _, _, resolver, _ = _use_case(pairs=pairs, concurrency=2)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 4)  # 每轮上限 concurrency*2
        self.assertEqual(len(resolver.calls), 4)
        self.assertLessEqual(resolver.max_inflight, 2)

    def test_ip_literal_skips_doh_direct_locate(self):
        geo = FakeGeo({"8.8.8.8": _US_GEO})
        uc, _, store, resolver, geo = _use_case(pairs=[_pair("8.8.8.8")], geo=geo)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [(("8.8.8.8",), "target")])
        self.assertEqual(store.rows[(_BIND, "8.8.8.8")]["country_code"], "US")

    def test_private_literals_and_intranet_names_skipped(self):
        pairs = [_pair("192.168.1.1"), _pair("10.0.0.5"), _pair("::1"),
                 _pair("intranet"), _pair("printer.local")]
        uc, _, store, resolver, geo = _use_case(pairs=pairs)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])
        self.assertEqual(geo.calls, [])
        self.assertEqual(store.saved, [])

    def test_empty_answer_writes_negative_row(self):
        resolver = FakeResolver({"nx.example": []})
        uc, _, store, resolver, geo = _use_case(pairs=[_pair("nx.example")], resolver=resolver)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(geo.calls, [])
        row = store.rows[(_BIND, "nx.example")]
        self.assertIsNone(row["ip"])
        self.assertIsNone(row["lat"])

    def test_geo_miss_writes_ip_only_row(self):
        resolver = FakeResolver({"cdn.example": ["203.0.113.9"]})
        uc, _, store, resolver, _ = _use_case(pairs=[_pair("cdn.example")], resolver=resolver)
        uc.touch()
        asyncio.run(uc.probe_cycle())
        row = store.rows[(_BIND, "cdn.example")]
        self.assertEqual(row["ip"], "203.0.113.9")
        self.assertIsNone(row["country_code"])
        self.assertIsNone(row["lat"])

    def test_geo_first_hit_within_two_addresses(self):
        resolver = FakeResolver({"multi.example": ["1.1.1.1", "2.2.2.2", "3.3.3.3"]})
        geo = FakeGeo({"2.2.2.2": _US_GEO})
        uc, _, store, resolver, geo = _use_case(pairs=[_pair("multi.example")],
                                                resolver=resolver, geo=geo)
        uc.touch()
        asyncio.run(uc.probe_cycle())
        self.assertEqual(len(geo.calls), 2)  # 只定位前两个 A 记录
        self.assertEqual(store.rows[(_BIND, "multi.example")]["ip"], "2.2.2.2")

    def test_candidate_snapshot_survives_queue_change(self):
        """候选阶段查到的代理随候选快照：探测前代理离队也不得用 None 发起请求"""
        proxies = {_BIND: _PROXY}
        uc, _, _, resolver, _ = _use_case(pairs=[_pair("example.com")], proxies=proxies)
        uc.touch()
        candidates = uc._pick_candidates(time.time())
        proxies.clear()  # 队列在候选选出后变化（代理离队）
        asyncio.run(uc._probe_pair(candidates[0], asyncio.Semaphore(1), time.time()))
        self.assertEqual(resolver.calls, [("example.com", _PROXY)])

    def test_bind_not_in_queue_skipped(self):
        uc, _, _, resolver, _ = _use_case(pairs=[_pair("example.com")], proxies={})
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])

    def test_invalid_hostname_skips_without_backoff(self):
        resolver = FakeResolver({"bad-.example": ValueError("IDNA 编码失败")})
        uc, _, store, resolver, _ = _use_case(pairs=[_pair("bad-.example")], resolver=resolver)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(uc._backoff, {}, "非法主机名跳过不记退避")
        self.assertEqual(store.saved, [])

    def test_cache_read_error_skips_cycle(self):
        uc, _, store, resolver, _ = _use_case(pairs=[_pair("example.com")])
        store.get_error = RuntimeError("db locked")
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 0)
        self.assertEqual(resolver.calls, [])

    def test_cache_write_error_does_not_raise(self):
        uc, _, store, resolver, _ = _use_case(pairs=[_pair("example.com")])
        store.save_error = RuntimeError("disk full")
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        self.assertEqual(store.saved, [])
        self.assertEqual(len(resolver.calls), 1)

    def test_hourly_prune_throttled_and_failure_tolerated(self):
        uc, _, store, _, _ = _use_case(pairs=[], activation_window=100000.0,
                                       prune_after=1000.0)
        with patch("src.use_cases.target_geo.time.time", return_value=10000.0):
            uc.touch()
            asyncio.run(uc.probe_cycle())
        self.assertEqual(len(store.pruned), 1)
        self.assertEqual(store.pruned[0], 9000.0)  # now - prune_after
        with patch("src.use_cases.target_geo.time.time", return_value=11000.0):
            asyncio.run(uc.probe_cycle())
        self.assertEqual(len(store.pruned), 1, "一小时内只清理一次")
        store.prune_error = RuntimeError("db busy")
        with patch("src.use_cases.target_geo.time.time", return_value=13600.0):
            asyncio.run(uc.probe_cycle())
        self.assertEqual(len(store.pruned), 1, "清理失败只记日志，不影响循环")


class TestStats(unittest.TestCase):
    """探测观测：累计成功/失败、退避计数与最近一轮快照（/api/globe 下发，不含 host）"""

    def test_inactive_snapshot_is_zeroed(self):
        uc, *_ = _use_case(pairs=[_pair("example.com")])
        st = uc.stats()
        self.assertFalse(st["active"])
        self.assertEqual((st["probe_ok"], st["probe_fail"], st["backoff"]), (0, 0, 0))
        self.assertEqual((st["candidates"], st["probed"], st["pending"]), (0, 0, 0))

    def test_success_and_failure_counters_with_backoff(self):
        resolver = FakeResolver({"bad.example": RuntimeError("dns down")})
        uc, *_ = _use_case(pairs=[_pair("ok.example"), _pair("bad.example")], resolver=resolver)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 2)
        st = uc.stats()
        self.assertTrue(st["active"])
        self.assertEqual((st["probe_ok"], st["probe_fail"]), (1, 1))
        self.assertEqual(st["backoff"], 1)
        self.assertEqual((st["candidates"], st["probed"], st["pending"]), (2, 2, 0))
        self.assertGreater(st["cycle_ts"], 0)

    def test_ip_literal_probe_not_counted_as_doh(self):
        """IP 字面量直接定位不算 DoH 探测（成功率分母只含真的 DoH 查询）"""
        geo = FakeGeo({"8.8.8.8": _US_GEO})
        uc, *_ = _use_case(pairs=[_pair("8.8.8.8")], geo=geo)
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 1)
        st = uc.stats()
        self.assertEqual((st["probe_ok"], st["probe_fail"]), (0, 0))
        self.assertEqual(st["probed"], 1)

    def test_pending_counts_due_beyond_batch_cap(self):
        now = time.time()
        pairs = [_pair(f"h{i}.example", last_seen=now + i) for i in range(5)]
        uc, *_ = _use_case(pairs=pairs, concurrency=1)  # 每轮上限 concurrency*2 = 2
        uc.touch()
        self.assertEqual(asyncio.run(uc.probe_cycle()), 2)
        st = uc.stats()
        self.assertEqual((st["candidates"], st["probed"], st["pending"]), (5, 2, 3))

    def test_snapshot_resets_but_counters_keep_when_inactive(self):
        uc, *_ = _use_case(pairs=[_pair("example.com")], activation_window=10.0)
        uc.touch()
        asyncio.run(uc.probe_cycle())
        future = time.time() + 999
        with patch("src.use_cases.target_geo.time.time", return_value=future):
            self.assertFalse(uc.stats()["active"])
            self.assertEqual(uc.stats()["probe_ok"], 1)   # 累计计数保留
            asyncio.run(uc.probe_cycle())                  # 未激活的一轮：清掉本轮快照
            st = uc.stats()
        self.assertEqual((st["candidates"], st["probed"], st["pending"]), (0, 0, 0))


class TestAggregateFlows(unittest.TestCase):
    """纯函数聚合：(bind, 城市) 分组、hosts 排序截断、无坐标不产弧、top N 截断"""

    @staticmethod
    def _row(cc="US", city="Ashburn", lat=39.0, lng=-77.5):
        return {"ip": "1.2.3.4", "country": "美国", "country_code": cc,
                "province": None, "city": city, "lat": lat, "lng": lng}

    def test_empty_inputs(self):
        self.assertEqual(TargetGeoUseCase.aggregate_flows([], {}, 10), [])

    def test_groups_by_bind_and_city_summing_rates(self):
        pairs = [_pair("a.example", rate=10.0), _pair("b.example", rate=5.0)]
        rows = {(_BIND, "a.example"): self._row(), (_BIND, "b.example"): self._row()}
        flows = TargetGeoUseCase.aggregate_flows(pairs, rows, 10)
        self.assertEqual(len(flows), 1)
        flow = flows[0]
        self.assertEqual(flow["bind"], _BIND)
        self.assertEqual(flow["up_rate"], 15.0)
        self.assertEqual(flow["down_rate"], 15.0)
        self.assertEqual(flow["up_inst"], 15.0)
        self.assertEqual(flow["hosts"], ["a.example", "b.example"])  # 按速率降序
        self.assertEqual((flow["lat"], flow["lng"]), (39.0, -77.5))

    def test_same_city_different_bind_stays_separate(self):
        pairs = [_pair("a.example"), _pair("a.example", bind="2.2.2.2:8080")]
        rows = {(_BIND, "a.example"): self._row(), ("2.2.2.2:8080", "a.example"): self._row()}
        flows = TargetGeoUseCase.aggregate_flows(pairs, rows, 10)
        self.assertEqual(len(flows), 2)
        self.assertEqual({f["bind"] for f in flows}, {_BIND, "2.2.2.2:8080"})

    def test_rows_without_coordinates_produce_no_arc(self):
        pairs = [_pair("a.example"), _pair("b.example")]
        rows = {(_BIND, "a.example"): {"ip": "1.2.3.4", "lat": None, "lng": None},
                (_BIND, "b.example"): None}
        self.assertEqual(TargetGeoUseCase.aggregate_flows(pairs, rows, 10), [])

    def test_hosts_capped_and_sorted(self):
        pairs = [_pair(f"h{i}.example", rate=float(i)) for i in range(10)]
        rows = {(_BIND, f"h{i}.example"): self._row() for i in range(10)}
        flows = TargetGeoUseCase.aggregate_flows(pairs, rows, 10)
        hosts = flows[0]["hosts"]
        self.assertEqual(len(hosts), 8)
        self.assertEqual(hosts[0], "h9.example")
        self.assertEqual(hosts[-1], "h2.example")  # 速率 0..9 的前 8 个：h9..h2

    def test_top_n_truncation_and_descending_order(self):
        pairs = [_pair("a.example", rate=1.0), _pair("a.example", bind="2.2.2.2:8080", rate=3.0),
                 _pair("a.example", bind="3.3.3.3:8080", rate=2.0)]
        rows = {(p["bind"], "a.example"): self._row(city=f"City{p['bind'][0]}") for p in pairs}
        flows = TargetGeoUseCase.aggregate_flows(pairs, rows, 2)
        self.assertEqual([f["bind"] for f in flows], ["2.2.2.2:8080", "3.3.3.3:8080"])
        self.assertEqual(flows[0]["up_rate"], 3.0)


class TestBuildFlows(unittest.TestCase):
    """/api/globe 用的 build_flows：批量读缓存后聚合；读失败降级为空、不抛"""

    def test_aggregates_from_cache(self):
        uc, _, store, *_ = _use_case(pairs=[], rows={(_BIND, "a.example"): dict(_US_ROW)})
        flows = uc.build_flows([_pair("a.example", rate=7.0)], 10)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["city"], "Ashburn")
        self.assertEqual(flows[0]["up_rate"], 7.0)
        self.assertEqual(store.gets, [[(_BIND, "a.example")]])

    def test_empty_pairs_skip_cache_read(self):
        uc, _, store, *_ = _use_case(pairs=[])
        self.assertEqual(uc.build_flows([], 10), [])
        self.assertEqual(store.gets, [])

    def test_cache_read_error_degrades_to_empty(self):
        """单次 DB 读失败只丢本帧目标弧，不让整个地球请求打成 500"""
        uc, _, store, *_ = _use_case(pairs=[], rows={(_BIND, "a.example"): dict(_US_ROW)})
        store.get_error = RuntimeError("db locked")
        self.assertEqual(uc.build_flows([_pair("a.example")], 10), [])


if __name__ == "__main__":
    unittest.main()
