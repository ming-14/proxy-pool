"""CheckPurityUseCase 单测：mock IpPurityChecker + PurityCacheStore

覆盖 IP 提取、缓存命中/过期/按源分区、结果归一化，以及三步编排的线程边界
（check_remote 为纯网络，不碰缓存）。
"""

import asyncio
import logging
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

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


class ResolveTest(unittest.TestCase):
    """async resolve/inspect 编排：缓存优先，网络在 executor、写缓存在调用线程（Web 与筛选共用）"""

    def test_resolve_cache_hit_skips_checker(self):
        cache = FakeCache()
        cache.store[("abuseipdb", "1.2.3.4")] = {
            "result": {"score": 5, "usage_type": "mobile", "extra": {}},
            "cached_at": time.time(),
        }
        checker = FakeChecker()
        result = asyncio.run(make_uc(checker, cache).resolve("http://1.2.3.4:80"))
        assert result["from_cache"] is True and result["score"] == 5
        assert checker.calls == []

    def test_resolve_miss_checks_and_saves(self):
        checker = FakeChecker()
        cache = FakeCache()
        result = asyncio.run(make_uc(checker, cache).resolve("http://1.2.3.4:80"))
        assert result["score"] == 0 and result["ip"] == "1.2.3.4"
        assert checker.calls == ["1.2.3.4"]
        assert cache.saves == 1  # 结果已落缓存

    def test_inspect_propagates_checker_error(self):
        """检查源异常原样抛出，由调用方（API 层）转 500 / 筛选放行"""
        uc = make_uc(FakeChecker())
        uc.check_remote = MagicMock(side_effect=RuntimeError("API error"))
        with self.assertRaises(RuntimeError):
            asyncio.run(uc.inspect("http://1.2.3.4:80"))

    def test_resolve_score_value_and_missing(self):
        """分数转交筛选：源未给出 score 时返回 None（判定放过）"""
        assert asyncio.run(make_uc(FakeChecker({"score": 42})).resolve_score(
            "http://1.2.3.4:80")) == 42
        assert asyncio.run(make_uc(FakeChecker({"usage_type": "hosting"})).resolve_score(
            "http://1.2.3.4:80")) is None


if __name__ == "__main__":
    unittest.main()
