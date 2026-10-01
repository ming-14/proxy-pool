"""PspeedTester 接口测试：mock pspeed 测量，验证缓存、串行与失败降级

_measure（真实 pspeed probe_one 调用）在本文件内被替换，测速不触网。
"""

import logging
import os
import sys
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.interfaces import speedtest
from src.interfaces.speedtest import PspeedTester


def _result(mbps=None, ok=True):
    """构造 _measure 返回结构（精简后的 download/upload）"""
    return {
        "proxy": "http://1.2.3.4:8080",
        "download": {"ok": ok, "best_mbps": mbps, "avg_mbps": mbps, "bytes": 1000,
                     "ms": 500.0, "node": "cloudflare", "err": None},
        "upload": None,
    }


class MeasureTest(unittest.TestCase):
    def test_measure_calls_pspeed_once(self):
        """面板手动测速：每次调用都真测（不读筛选缓存）"""
        with patch.object(speedtest, "_measure", return_value=_result(5.0)) as m:
            tester = PspeedTester()
            assert tester.measure("http://1.2.3.4:8080")["download"]["best_mbps"] == 5.0
            tester.measure("http://1.2.3.4:8080")
        assert m.call_count == 2


class DownloadMbpsTest(unittest.TestCase):
    def test_returns_best_mbps(self):
        with patch.object(speedtest, "_measure", return_value=_result(7.5)):
            assert PspeedTester().download_mbps("http://1.2.3.4:8080") == 7.5

    def test_caches_within_ttl(self):
        """TTL 内同一代理只测一次（入队筛选每轮都会问同一候选）"""
        with patch.object(speedtest, "_measure", return_value=_result(7.5)) as m:
            tester = PspeedTester(cache_ttl=3600)
            assert tester.download_mbps("http://1.2.3.4:8080") == 7.5
            assert tester.download_mbps("http://1.2.3.4:8080") == 7.5
        assert m.call_count == 1

    def test_ttl_expired_remeasures(self):
        with patch.object(speedtest, "_measure", return_value=_result(7.5)) as m:
            tester = PspeedTester(cache_ttl=0)
            tester.download_mbps("http://1.2.3.4:8080")
            tester.download_mbps("http://1.2.3.4:8080")
        assert m.call_count == 2

    def test_failure_not_cached(self):
        """测不出（ok=False/无速率）：返回 None 且不写缓存，下轮可重测"""
        with patch.object(speedtest, "_measure", return_value=_result(ok=False)) as m:
            tester = PspeedTester()
            assert tester.download_mbps("http://1.2.3.4:8080") is None
            assert tester.download_mbps("http://1.2.3.4:8080") is None
        assert m.call_count == 2

    def test_missing_rate_returns_none(self):
        with patch.object(speedtest, "_measure", return_value=_result(mbps=None)):
            assert PspeedTester().download_mbps("http://1.2.3.4:8080") is None

    def test_concurrent_same_proxy_measures_once(self):
        """并发问同一候选：等锁者复检缓存，不重复测（10s + 100MB 级不能翻倍）"""
        started = threading.Event()
        release = threading.Event()

        def slow_measure(proxy):
            started.set()
            release.wait(timeout=5)
            return _result(3.0)

        with patch.object(speedtest, "_measure", side_effect=slow_measure) as m:
            tester = PspeedTester()
            threads = [threading.Thread(target=tester.download_mbps, args=("http://1.2.3.4:8080",))
                       for _ in range(3)]
            threads[0].start()
            assert started.wait(timeout=5)
            for t in threads[1:]:
                t.start()
            time.sleep(0.05)  # 让其余线程进入等锁分支
            release.set()
            for t in threads:
                t.join(timeout=5)
        assert m.call_count == 1


class LazyImportTest(unittest.TestCase):
    def test_pspeed_not_imported_at_module_import(self):
        """导入本模块只注册 vendor 路径，不拉起 pspeed/requests 依赖链"""
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import src.interfaces.speedtest;"
            "bad = [m for m in ('pspeed', 'requests') if m in sys.modules];"
            "print(bad); sys.exit(1 if bad else 0)"
        )
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        r = subprocess.run([sys.executable, "-c", code, root], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"顶层导入拉起测速依赖链: {r.stdout.strip()}"


if __name__ == "__main__":
    unittest.main()
