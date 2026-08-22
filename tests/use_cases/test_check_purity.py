"""CheckPurityUseCase 单测：mock IpPurityChecker + PurityCacheStore，验证 IP 提取、缓存命中/未命中"""

import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.use_cases.check_purity import CheckPurityUseCase, _extract_ip


class FakeChecker:
    """IpPurityChecker 假实现：记录调用参数，返回固定结果"""

    def __init__(self, result=None):
        self.calls = []
        self._result = result or {
            "abuse_confidence_score": 0,
            "usage_type": "Fixed Line ISP",
            "country_code": "DE",
        }

    def check(self, ip, *, max_age_in_days=90):
        self.calls.append((ip, max_age_in_days))
        return dict(self._result)


class FakeCache:
    """PurityCacheStore 假实现：内存 dict 模拟"""

    def __init__(self):
        self.store = {}   # ip → {"result": {...}, "cached_at": float}

    def get_purity(self, ip):
        return self.store.get(ip)

    def save_purity(self, ip, result):
        self.store[ip] = {"result": dict(result), "cached_at": time.time()}


class ExtractIpTest(unittest.TestCase):
    def test_ipv4(self):
        assert _extract_ip("1.2.3.4:8080") == "1.2.3.4"

    def test_ipv6_bracketed(self):
        assert _extract_ip("[::1]:8080") == "::1"

    def test_bare_ip(self):
        assert _extract_ip("1.2.3.4") == "1.2.3.4"


class CheckPurityUseCaseTest(unittest.TestCase):
    def test_execute_extracts_ip_from_full_url(self):
        """完整 proxy URL → 提取 IP → 调 checker"""
        checker = FakeChecker()
        uc = CheckPurityUseCase(checker, max_age_in_days=90)
        result = uc.execute("http://1.2.3.4:8080")
        assert checker.calls == [("1.2.3.4", 90)]
        assert result["ip"] == "1.2.3.4"
        assert result["abuse_confidence_score"] == 0
        assert result["usage_type"] == "Fixed Line ISP"

    def test_execute_extracts_ip_from_bind(self):
        """裸 bind（无 scheme）也能提取 IP"""
        checker = FakeChecker()
        uc = CheckPurityUseCase(checker)
        uc.execute("5.6.7.8:3128")
        assert checker.calls == [("5.6.7.8", 90)]

    def test_execute_passes_max_age(self):
        """max_age_in_days 透传到 checker"""
        checker = FakeChecker()
        uc = CheckPurityUseCase(checker, max_age_in_days=30)
        uc.execute("http://1.1.1.1:80")
        assert checker.calls[0][1] == 30

    def test_execute_returns_full_result(self):
        """checker 返回的所有字段都透传到结果"""
        checker = FakeChecker(result={
            "abuse_confidence_score": 100,
            "usage_type": "Data Center/Web Hosting/Transit",
            "country_code": "US",
            "isp": "TestISP",
            "total_reports": 42,
        })
        uc = CheckPurityUseCase(checker)
        result = uc.execute("http://9.9.9.9:80")
        assert result["abuse_confidence_score"] == 100
        assert result["usage_type"] == "Data Center/Web Hosting/Transit"
        assert result["isp"] == "TestISP"
        assert result["total_reports"] == 42
        assert result["ip"] == "9.9.9.9"


class CheckPurityCacheTest(unittest.TestCase):
    """缓存逻辑测试：命中/未命中/过期"""

    def test_cache_miss_calls_checker_and_writes_cache(self):
        """未命中：调 checker + 写缓存"""
        checker = FakeChecker()
        cache = FakeCache()
        uc = CheckPurityUseCase(checker, cache, cache_ttl=30 * 86400)
        result = uc.execute("http://1.2.3.4:80")
        assert checker.calls == [("1.2.3.4", 90)]
        assert "1.2.3.4" in cache.store
        assert result.get("from_cache") is None

    def test_cache_hit_skips_checker(self):
        """命中：不调 checker，返回 from_cache=True"""
        checker = FakeChecker()
        cache = FakeCache()
        cache.store["1.2.3.4"] = {
            "result": {"abuse_confidence_score": 5, "usage_type": "Mobile ISP"},
            "cached_at": time.time(),
        }
        uc = CheckPurityUseCase(checker, cache, cache_ttl=30 * 86400)
        result = uc.execute("http://1.2.3.4:80")
        assert checker.calls == []
        assert result["from_cache"] is True
        assert result["abuse_confidence_score"] == 5

    def test_cache_expired_calls_checker(self):
        """缓存过期：重新调 checker"""
        checker = FakeChecker()
        cache = FakeCache()
        cache.store["1.2.3.4"] = {
            "result": {"abuse_confidence_score": 5},
            "cached_at": time.time() - 31 * 86400,
        }
        uc = CheckPurityUseCase(checker, cache, cache_ttl=30 * 86400)
        result = uc.execute("http://1.2.3.4:80")
        assert checker.calls == [("1.2.3.4", 90)]
        assert result.get("from_cache") is None

    def test_no_cache_always_calls_checker(self):
        """cache=None：每次都调 checker"""
        checker = FakeChecker()
        uc = CheckPurityUseCase(checker, cache=None)
        uc.execute("http://1.2.3.4:80")
        uc.execute("http://1.2.3.4:80")
        assert len(checker.calls) == 2


if __name__ == "__main__":
    unittest.main()
