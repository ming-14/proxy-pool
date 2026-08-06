"""JsonProxyStore 文件存储测试（原 test_cache.py 持久化部分，8 用例 + 契约用例）"""

import json
import logging
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.interfaces.json_store import JsonProxyStore
from src.framework.config import (
    MAX_FAILS,
    TRUSTED_MAX_FAILS,
    SOURCE_PAUSE_TTL,
    PROXY_GROUPS,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
)


def make_pool():
    """构造默认配置的 ProxyPool（等价组合根的行为）"""
    return ProxyPool(
        PROXY_GROUPS,
        max_fails=MAX_FAILS,
        trusted_max_fails=TRUSTED_MAX_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "freeproxy-world-http", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


class TestJsonProxyStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")
        self.store = JsonProxyStore(self.cache_file)

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_save_and_load(self):
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=1)
        pool.add_to_group("freeproxy-world-http", "http://5.6.7.8:3128")
        pool.add_to_group("trusted", "http://9.9.9.9:80", fails=2)
        pool.add_to_group("blacklist", "http://10.10.10.10:80")
        self.store.save(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://5.6.7.8:3128", pool2.proxy_groups["freeproxy-world-http"]["proxies"])
        self.assertIn("http://9.9.9.9:80", pool2.proxy_groups["trusted"]["proxies"])
        self.assertIn("http://10.10.10.10:80", pool2.proxy_groups["blacklist"]["proxies"])

    def test_load_filters_expired(self):
        pool = make_pool()
        pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "fails": 0
        }
        pool.add_to_group("trusted", "http://9.9.9.9:80")
        self.store.save(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertNotIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://9.9.9.9:80", pool2.proxy_groups["trusted"]["proxies"])

    def test_load_corrupt_file(self):
        with open(self.cache_file, "w") as f:
            f.write("not valid json{{{")
        data = self.store.load()
        self.assertEqual(data, {})
        pool = make_pool()
        pool.restore_state(data)
        for name in PROXY_GROUPS:
            self.assertIn(name, pool.proxy_groups)

    def test_load_nonexistent_file(self):
        store = JsonProxyStore(os.path.join(self.tmp_dir, "no_such_file"))
        pool = make_pool()
        pool.restore_state(store.load())
        for name in PROXY_GROUPS:
            self.assertIn(name, pool.proxy_groups)

    def test_save_preserves_fails(self):
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080", fails=2)
        self.store.save(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertEqual(pool2.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["fails"], 2)

    def test_source_pause_persisted(self):
        pool = make_pool()
        pool.pause_source("src.interfaces.sources.ip89")
        self.store.save(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertIn("src.interfaces.sources.ip89", pool2.source_paused)

    def test_source_pause_expired_not_persisted(self):
        pool = make_pool()
        pool.source_paused["src.interfaces.sources.ip89"] = {
            "paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None
        }
        self.store.save(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertNotIn("src.interfaces.sources.ip89", pool2.source_paused)

    def test_save_load_round_trip_many(self):
        pool = make_pool()
        for i in range(50):
            pool.add_to_group("default-http", f"http://{i}.0.0.0:8080", fails=i % 3)
        self.store.save(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertEqual(len(pool2.proxy_groups["default-http"]["proxies"]), 50)


class TestJsonProxyStoreContract(unittest.TestCase):
    """存储契约：与领域模型解耦的纯读写行为"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")
        self.store = JsonProxyStore(self.cache_file)

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_load_corrupt_file_returns_empty(self):
        with open(self.cache_file, "w") as f:
            f.write("{oops")
        self.assertEqual(self.store.load(), {})

    def test_save_creates_file(self):
        self.store.save({"proxy_groups": {}, "source_paused": {}})
        self.assertTrue(os.path.exists(self.cache_file))

    def test_save_load_round_trip_raw(self):
        state = {"proxy_groups": {"default-http": {"ttl": 240, "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": 1700000000, "fails": 0}]}}, "source_paused": {}}
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_save_compact_json(self):
        """文件为紧凑 JSON（无多余空格），ensure_ascii=False"""
        self.store.save({"proxy_groups": {}, "source_paused": {"src.interfaces.sources.geonode": {"paused_at": 1, "ttl": 2}}})
        with open(self.cache_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(", ", raw)
        self.assertNotIn(": ", raw)
        self.assertIn("geonode", raw)


if __name__ == "__main__":
    unittest.main()
