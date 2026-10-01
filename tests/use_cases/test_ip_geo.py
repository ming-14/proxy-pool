"""IpGeoUseCase 单测：mock GeoLocator + GeoCacheStore，验证 IP 收集、缓存、聚合、resolve 编排"""

import asyncio
import logging
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.use_cases.ip_geo import IpGeoUseCase

# 测试用池配置：两个组，验证跨组去重
_TEST_GROUPS = {
    "default-http": None,
    "default-https": None,
}


def make_pool() -> ProxyPool:
    pool = ProxyPool(
        _TEST_GROUPS,
        max_consecutive_fails=5,
        trusted_max_consecutive_fails=10,
        default_pause_ttl=1200,
        requestable_groups=("default-http", "default-https"),
        trusted_group="trusted",
        blacklist_group="blacklist",
    )
    pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=0)
    pool.add_to_group("default-http", "http://5.6.7.8:3128", consecutive_fails=0)
    pool.add_to_group("default-https", "https://1.2.3.4:8443", consecutive_fails=0)  # 跨组副本
    pool.add_to_group("default-https", "https://9.9.9.9:443", consecutive_fails=0)
    return pool


class FakeLocator:
    """GeoLocator 假实现：静态 IP → geo 映射，未知 IP 返回 None"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {
            "1.2.3.4": {"country": "中国", "country_code": "CN", "province": "上海市", "city": "上海",
                        "lat": 31.23, "lng": 121.47},
            "5.6.7.8": {"country": "中国", "country_code": "CN", "province": "广东省", "city": "深圳",
                        "lat": 22.54, "lng": 114.06},
            "9.9.9.9": {"country": "美国", "country_code": "US", "province": "California", "city": "Los Angeles",
                        "lat": 34.05, "lng": -118.24},
        }
        self.calls: list[str] = []

    def locate(self, ip: str) -> dict | None:
        self.calls.append(ip)
        return self.mapping.get(ip)


class FakeGeoCache:
    """GeoCacheStore 假实现：内存 dict 模拟"""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.fingerprint: str | None = None

    def get_geo_batch(self, ips):
        return {ip: self.store[ip] for ip in ips if ip in self.store}

    def save_geo_batch(self, results):
        for ip, d in results.items():
            self.store[ip] = dict(d)

    def get_geo_cache_fingerprint(self):
        return self.fingerprint

    def set_geo_cache_fingerprint(self, fingerprint):
        self.fingerprint = fingerprint

    def clear_geo_cache(self):
        self.store.clear()


class CollectIpsTest(unittest.TestCase):
    def test_collects_unique_ips_across_groups(self):
        """跨组副本只计一次，scheme 前缀正确剥离"""
        ips = IpGeoUseCase.collect_ips(make_pool())
        assert sorted(ips) == ["1.2.3.4", "5.6.7.8", "9.9.9.9"]


class QueryCacheTest(unittest.TestCase):
    def test_miss_returns_all_missing(self):
        uc = IpGeoUseCase(FakeLocator(), FakeGeoCache())
        valid, missing = uc.query_cache(["1.2.3.4", "5.6.7.8"])
        assert valid == {}
        assert missing == ["1.2.3.4", "5.6.7.8"]

    def test_hit_returns_cached_and_no_missing(self):
        cache = FakeGeoCache()
        cache.store["1.2.3.4"] = {"country": "中国", "country_code": "CN", "lat": 31.23, "lng": 121.47,
                                  "cached_at": time.time()}
        uc = IpGeoUseCase(FakeLocator(), cache)
        valid, missing = uc.query_cache(["1.2.3.4"])
        assert missing == []
        assert valid["1.2.3.4"]["country"] == "中国"

    def test_expired_returns_missing(self):
        cache = FakeGeoCache()
        cache.store["1.2.3.4"] = {"country": "中国", "country_code": "CN", "lat": 31.23, "lng": 121.47,
                                  "cached_at": time.time() - 100 * 86400}
        uc = IpGeoUseCase(FakeLocator(), cache, cache_ttl=90 * 86400)
        valid, missing = uc.query_cache(["1.2.3.4"])
        assert valid == {}
        assert missing == ["1.2.3.4"]

    def test_no_cache_returns_all_missing(self):
        uc = IpGeoUseCase(FakeLocator(), cache=None)
        valid, missing = uc.query_cache(["1.2.3.4"])
        assert valid == {}
        assert missing == ["1.2.3.4"]


class LocateTest(unittest.TestCase):
    def test_locate_returns_only_resolved(self):
        locator = FakeLocator()
        uc = IpGeoUseCase(locator)
        results = uc.locate(["1.2.3.4", "10.0.0.1"])  # 10.0.0.1 保留地址无记录
        assert list(results.keys()) == ["1.2.3.4"]
        assert locator.calls == ["1.2.3.4", "10.0.0.1"]

    def test_save_cache_writes(self):
        cache = FakeGeoCache()
        uc = IpGeoUseCase(FakeLocator(), cache)
        uc.save_cache({"1.2.3.4": {"country": "中国", "country_code": "CN", "lat": 31.23, "lng": 121.47}})
        assert "1.2.3.4" in cache.store


class AggregateTest(unittest.TestCase):
    def test_aggregates_by_city_with_count(self):
        """同城多 IP 聚合成一个点，count 累加"""
        geo_map = {
            "1.2.3.4": {"country": "中国", "country_code": "CN", "province": "上海市", "city": "上海",
                        "lat": 31.23, "lng": 121.47},
            "1.2.3.5": {"country": "中国", "country_code": "CN", "province": "上海市", "city": "上海",
                        "lat": 31.23, "lng": 121.47},
            "9.9.9.9": {"country": "美国", "country_code": "US", "province": "California", "city": "Los Angeles",
                        "lat": 34.05, "lng": -118.24},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert len(agg["points"]) == 2
        sh = next(p for p in agg["points"] if p["country_code"] == "CN")
        assert sh["name"] == "上海市上海" and sh["count"] == 2
        assert sh["lat"] == 31.23 and sh["lng"] == 121.47

    def test_fallback_province_when_no_city(self):
        """无城市时聚合到省"""
        geo_map = {
            "1.1.1.1": {"country": "中国", "country_code": "CN", "province": "四川省", "city": None,
                        "lat": 30.65, "lng": 104.06},
            "1.1.1.2": {"country": "中国", "country_code": "CN", "province": "四川省", "city": None,
                        "lat": 30.65, "lng": 104.06},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert len(agg["points"]) == 1
        assert agg["points"][0]["name"] == "四川省" and agg["points"][0]["count"] == 2

    def test_fallback_country_when_no_province(self):
        """无省市时聚合到国家"""
        geo_map = {
            "2.2.2.2": {"country": "卢森堡", "country_code": "LU", "province": None, "city": None,
                        "lat": 49.61, "lng": 6.13},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert agg["points"][0]["name"] == "卢森堡" and agg["points"][0]["count"] == 1

    def test_countries_summary_sorted(self):
        """国家汇总按 count 降序"""
        geo_map = {
            "1.2.3.4": {"country": "中国", "country_code": "CN", "province": "上海市", "city": "上海",
                        "lat": 31.23, "lng": 121.47},
            "1.2.3.5": {"country": "中国", "country_code": "CN", "province": "广东省", "city": "深圳",
                        "lat": 22.54, "lng": 114.06},
            "9.9.9.9": {"country": "美国", "country_code": "US", "province": "California", "city": "Los Angeles",
                        "lat": 34.05, "lng": -118.24},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert agg["countries"][0] == {"name": "中国", "code": "CN", "count": 2}
        assert agg["countries"][1] == {"name": "美国", "code": "US", "count": 1}

    def test_points_sorted_by_count_desc(self):
        """点按 count 降序排列"""
        geo_map = {
            "1.1.1.1": {"country": "A", "country_code": "AA", "province": None, "city": "c1",
                        "lat": 1.0, "lng": 1.0},
            "2.2.2.2": {"country": "A", "country_code": "AA", "province": None, "city": "c2",
                        "lat": 2.0, "lng": 2.0},
            "2.2.2.3": {"country": "A", "country_code": "AA", "province": None, "city": "c2",
                        "lat": 2.0, "lng": 2.0},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert agg["points"][0]["name"] == "c2" and agg["points"][0]["count"] == 2
        assert agg["points"][1]["name"] == "c1" and agg["points"][1]["count"] == 1

    def test_country_db_falls_back_to_national_center(self):
        """Country 库（无坐标）：散点用国家中心坐标兜底，聚合到国家粒度"""
        geo_map = {
            "8.8.8.8": {"country": "美国", "country_code": "US", "province": None, "city": None,
                        "lat": None, "lng": None},
            "8.8.4.4": {"country": "美国", "country_code": "US", "province": None, "city": None,
                        "lat": None, "lng": None},
            "114.114.114.114": {"country": "中国", "country_code": "CN", "province": None, "city": None,
                                "lat": None, "lng": None},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert len(agg["points"]) == 2
        us = next(p for p in agg["points"] if p["country_code"] == "US")
        assert us["name"] == "美国" and us["count"] == 2
        assert us["lat"] == 39.8 and us["lng"] == -98.6  # 美国中心坐标
        cn = next(p for p in agg["points"] if p["country_code"] == "CN")
        assert cn["count"] == 1 and cn["lat"] == 35.0 and cn["lng"] == 105.0
        assert agg["countries"][0] == {"name": "美国", "code": "US", "count": 2}

    def test_country_db_without_center_skips_point(self):
        """无坐标且国家中心表无记录：不进散点，仅计入国家级汇总"""
        geo_map = {
            "1.1.1.1": {"country": "某未知国", "country_code": "ZZ", "province": None, "city": None,
                        "lat": None, "lng": None},
        }
        agg = IpGeoUseCase.aggregate(geo_map)
        assert agg["points"] == []
        assert agg["countries"] == [{"name": "某未知国", "code": "ZZ", "count": 1}]


class ResolveTest(unittest.TestCase):
    """async resolve 编排：缓存优先；三步各自降级（Web 面板与入队筛选共用）"""

    def test_cache_hit_skips_locate(self):
        cache = FakeGeoCache()
        cache.store["1.2.3.4"] = {"country": "中国", "country_code": "CN", "lat": 31.23, "lng": 121.47,
                                  "cached_at": time.time()}
        locator = FakeLocator()
        geo = asyncio.run(IpGeoUseCase(locator, cache).resolve(["1.2.3.4"], label="t"))
        assert geo["1.2.3.4"]["country"] == "中国"
        assert locator.calls == []  # 缓存命中不碰 mmdb

    def test_miss_locates_and_saves(self):
        cache = FakeGeoCache()
        geo = asyncio.run(
            IpGeoUseCase(FakeLocator(), cache).resolve(["1.2.3.4", "10.0.0.1"], label="t"))
        assert set(geo) == {"1.2.3.4"}  # 未定位到的 IP 不出现在结果里
        assert "1.2.3.4" in cache.store  # 定位结果已落缓存

    def test_cache_read_failure_returns_empty(self):
        cache = FakeGeoCache()
        cache.get_geo_batch = MagicMock(side_effect=RuntimeError("db 挂了"))
        geo = asyncio.run(IpGeoUseCase(FakeLocator(), cache).resolve(["1.2.3.4"], label="t"))
        assert geo == {}

    def test_locate_failure_keeps_cached(self):
        cache = FakeGeoCache()
        cache.store["1.2.3.4"] = {"country": "中国", "country_code": "CN", "lat": 31.23, "lng": 121.47,
                                  "cached_at": time.time()}
        locator = FakeLocator()
        locator.locate = MagicMock(side_effect=RuntimeError("mmdb 挂了"))
        geo = asyncio.run(IpGeoUseCase(locator, cache).resolve(["1.2.3.4", "5.6.7.8"], label="t"))
        assert list(geo) == ["1.2.3.4"]  # 已缓存 IP 照常下发，缺失 IP 不连坐

    def test_save_cache_failure_keeps_result(self):
        cache = FakeGeoCache()
        cache.save_geo_batch = MagicMock(side_effect=RuntimeError("磁盘满了"))
        geo = asyncio.run(IpGeoUseCase(FakeLocator(), cache).resolve(["1.2.3.4"], label="t"))
        assert geo["1.2.3.4"]["city"] == "上海"  # 只丢持久化，不丢本次结果


class ResolveCountryTest(unittest.TestCase):
    """单代理国家查询：入队筛选国家指标的测量源"""

    def test_accepts_url_and_bind(self):
        uc = IpGeoUseCase(FakeLocator(), FakeGeoCache())
        assert asyncio.run(uc.resolve_country("http://1.2.3.4:8080")) == "CN"
        assert asyncio.run(uc.resolve_country("9.9.9.9:443")) == "US"

    def test_unknown_ip_returns_none(self):
        """无定位记录返回 None（判定层按未测到放过）"""
        uc = IpGeoUseCase(FakeLocator(), FakeGeoCache())
        assert asyncio.run(uc.resolve_country("10.0.0.1:8080")) is None


if __name__ == "__main__":
    unittest.main()
