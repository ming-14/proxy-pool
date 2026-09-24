"""CheckPurityUseCase 单测：mock IpPurityChecker + PurityCacheStore

覆盖 IP 提取、缓存命中/过期/按源分区、结果归一化，以及三步编排的线程边界
（check_remote 为纯网络，不碰缓存）。
"""

import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.domain.models import proxy_to_ip
from src.domain.purity import UsageType
from src.use_cases.check_purity import CheckPurityUseCase


class FakeChecker:
    """IpPurityChecker 假实现：记录被查 IP，返回固定结果"""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {
            "score": 0,
            "usage_type": UsageType.RESIDENTIAL,
            "extra": {"isp": "TestISP"},
        }

    def check(self, ip):
        self.calls.append(ip)
        return dict(self._result)


class FakeCache:
    """PurityCacheStore 假实现：内存 dict 模拟，键为 (source, ip)"""

    def __init__(self):
        self.store = {}
        self.gets = 0
        self.saves = 0

    def get_purity(self, source, ip):
        self.gets += 1
        return self.store.get((source, ip))

    def save_purity(self, source, ip, result):
        self.saves += 1
        self.store[(source, ip)] = {"result": dict(result), "cached_at": time.time()}


def make_uc(checker, cache=None, *, source="abuseipdb", cache_ttl=30 * 86400):
    return CheckPurityUseCase(checker, source=source, label="AbuseIPDB",
                              cache_ttl=cache_ttl, cache=cache)


class ExtractIpTest(unittest.TestCase):
    def test_ipv4(self):
        assert proxy_to_ip("1.2.3.4:8080") == "1.2.3.4"

    def test_ipv6_bracketed(self):
        assert proxy_to_ip("[::1]:8080") == "::1"

    def test_bare_ip(self):
        assert proxy_to_ip("1.2.3.4") == "1.2.3.4"


class CheckRemoteTest(unittest.TestCase):
    """check_remote 为纯网络步骤：不提 IP 提取的错，且不碰缓存"""

    def test_extracts_ip_from_full_url(self):
        checker = FakeChecker()
        result = make_uc(checker).check_remote("http://1.2.3.4:8080")
        assert checker.calls == ["1.2.3.4"]
        assert result["ip"] == "1.2.3.4"
        assert result["score"] == 0
        assert result["usage_type"] == "residential"
        assert result["extra"] == {"isp": "TestISP"}

    def test_extracts_ip_from_bind(self):
        checker = FakeChecker()
        make_uc(checker).check_remote("5.6.7.8:3128")
        assert checker.calls == ["5.6.7.8"]

    def test_does_not_touch_cache(self):
        cache = FakeCache()
        make_uc(FakeChecker(), cache).check_remote("http://1.2.3.4:80")
        assert (cache.gets, cache.saves) == (0, 0)

    def test_result_is_normalized(self):
        """插件返回脏值 → 收敛为通用契约后才出用例层"""
        checker = FakeChecker({"score": 999, "usage_type": "satellite", "extra": None})
        result = make_uc(checker).check_remote("http://1.2.3.4:80")
        assert result["score"] is None
        assert result["usage_type"] == "unknown"
        assert result["extra"] == {}


class CacheTest(unittest.TestCase):
    """缓存：命中/未命中/过期/按源分区"""

    def test_no_cache_configured(self):
        assert make_uc(FakeChecker()).query_cache("http://1.2.3.4:80") is None

    def test_miss_then_check_then_save(self):
        """未命中：check_remote 调源、save_cache 写入（键含源名）"""
        checker = FakeChecker()
        cache = FakeCache()
        uc = make_uc(checker, cache)
        result = uc.check_remote("http://1.2.3.4:80")
        uc.save_cache("http://1.2.3.4:80", result)
        assert checker.calls == ["1.2.3.4"]
        assert cache.saves == 1
        assert ("abuseipdb", "1.2.3.4") in cache.store

    def test_hit_returns_from_cache(self):
        """命中：返回缓存结果（from_cache=True）且带 ip"""
        checker = FakeChecker()
        cache = FakeCache()
        cache.store[("abuseipdb", "1.2.3.4")] = {
            "result": {"score": 5, "usage_type": "mobile", "extra": {}},
            "cached_at": time.time(),
        }
        result = make_uc(checker, cache).query_cache("http://1.2.3.4:80")
        assert checker.calls == []
        assert result["from_cache"] is True
        assert result["score"] == 5
        assert result["ip"] == "1.2.3.4"

    def test_expired_cache_misses(self):
        """过期：不返回缓存（由调用方重新实查）"""
        cache = FakeCache()
        cache.store[("abuseipdb", "1.2.3.4")] = {
            "result": {"score": 5},
            "cached_at": time.time() - 31 * 86400,
        }
        assert make_uc(FakeChecker(), cache, cache_ttl=30 * 86400).query_cache(
            "http://1.2.3.4:80") is None

    def test_cache_partitioned_by_source(self):
        """换源后不复用旧源结果"""
        cache = FakeCache()
        cache.store[("abuseipdb", "1.2.3.4")] = {
            "result": {"score": 90}, "cached_at": time.time(),
        }
        assert make_uc(FakeChecker(), cache, source="ipok").query_cache(
            "http://1.2.3.4:80") is None

    def test_zero_ttl_never_hits(self):
        """cache_ttl=0 表示不缓存：写入后立刻读也为未命中"""
        cache = FakeCache()
        uc = make_uc(FakeChecker(), cache, cache_ttl=0)
        uc.save_cache("http://1.2.3.4:80", {"score": 1})
        assert uc.query_cache("http://1.2.3.4:80") is None


class MetadataTest(unittest.TestCase):
    """源元数据：供缓存分区与面板标注数据来源"""

    def test_source_and_label(self):
        uc = make_uc(FakeChecker(), source="ipok")
        assert uc.source == "ipok"
        assert uc.label == "AbuseIPDB"


if __name__ == "__main__":
    unittest.main()
