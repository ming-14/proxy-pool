"""纯净度插件公共骨架单测：惰性导入守卫与 compact 过滤"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from src.interfaces.ippure.base import compact  # noqa: E402


class CompactTest(unittest.TestCase):
    def test_drops_none(self):
        assert compact({"a": 1, "b": None, "c": False, "d": 0}) == {"a": 1, "c": False, "d": 0}

    def test_empty(self):
        assert compact({}) == {}


class LazyImportTest(unittest.TestCase):
    def test_plugin_modules_do_not_import_requests(self):
        """插件模块导入不得拉起 requests/urllib3（纯净度禁用时不能白载依赖链）"""
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import src.interfaces.ippure.abuseipdb, src.interfaces.ippure.ipok;"
            "print([m for m in ('requests', 'urllib3', 'aiohttp') if m in sys.modules])"
        )
        r = subprocess.run([sys.executable, "-c", code, str(PROJECT_ROOT)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]", f"插件导入拉起重依赖: {r.stdout.strip()}"


if __name__ == "__main__":
    unittest.main()
