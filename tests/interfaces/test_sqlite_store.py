"""SqliteProxyStore SQLite 存储测试（WAL 模式 + 组级增量 upsert）"""

import logging
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.interfaces.sqlite_store import SqliteProxyStore
from src.framework.config import (
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    PROXY_GROUPS,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
)


def make_pool():
    """构造默认配置的 ProxyPool（等价组合根的行为）"""
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "freeproxy-world-http", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


def _cleanup_db(db_path: str) -> None:
    """删除 SQLite 数据库及其 WAL/SHM 侧车文件"""
    for ext in ("", "-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)


class TestSqliteProxyStore(unittest.TestCase):
    """存储契约：纯读写行为（与领域模型解耦）"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, ".cache.db")
        self.store = SqliteProxyStore(self.db_path)

    def tearDown(self):
        self.store.close()
        _cleanup_db(self.db_path)
        os.rmdir(self.tmp_dir)

    def test_load_empty(self):
        """新建 db，load 返回空 dict"""
        self.assertEqual(self.store.load(), {})

    def test_save_full_load_roundtrip(self):
        """save_full 后 load 还原（含 latency/pinned/source_paused）"""
        state = {
            "proxy_groups": {
                "default-http": {"ttl": 3600, "proxies": [
                    {"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 1, "latency": 0.5}
                ]},
                "trusted": {"ttl": None, "proxies": [
                    {"proxy": "http://9.9.9.9:80", "joined_at": time.time(), "consecutive_fails": 0, "pinned": True, "pinned_at": 123}
                ]},
                "blacklist": {"ttl": 10800, "proxies": [
                    {"proxy": "http://10.10.10.10:80", "joined_at": time.time(), "consecutive_fails": 0, "source_group": "default-http"}
                ]},
            },
            "source_paused": {"src.interfaces.sources.ip89": {"paused_at": time.time(), "ttl": 1200}},
        }
        self.store.save_full(state)
        loaded = self.store.load()
        dh = loaded["proxy_groups"]["default-http"]["proxies"][0]
        self.assertEqual(dh["proxy"], "http://1.2.3.4:8080")
        self.assertEqual(dh["consecutive_fails"], 1)
        self.assertEqual(dh["latency"], 0.5)
        tr = loaded["proxy_groups"]["trusted"]["proxies"][0]
        self.assertEqual(tr["pinned"], True)
        self.assertEqual(tr["pinned_at"], 123)
        bl = loaded["proxy_groups"]["blacklist"]["proxies"][0]
        self.assertEqual(bl["source_group"], "default-http")
        self.assertIn("src.interfaces.sources.ip89", loaded["source_paused"])

    def test_save_group_incremental(self):
        """save_group 只更新该组，其他组不变"""
        self.store.save_full({
            "proxy_groups": {
                "default-http": {"ttl": 3600, "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 0}]},
                "trusted": {"ttl": None, "proxies": [{"proxy": "http://9.9.9.9:80", "joined_at": time.time(), "consecutive_fails": 0}]},
            },
            "source_paused": {},
        })
        self.store.save_group("default-http", {"ttl": 3600, "proxies": [
            {"proxy": "http://5.6.7.8:3128", "joined_at": time.time(), "consecutive_fails": 2}
        ]})
        loaded = self.store.load()
        proxies_dh = loaded["proxy_groups"]["default-http"]["proxies"]
        self.assertEqual(len(proxies_dh), 1)
        self.assertEqual(proxies_dh[0]["proxy"], "http://5.6.7.8:3128")
        proxies_t = loaded["proxy_groups"]["trusted"]["proxies"]
        self.assertEqual(len(proxies_t), 1)
        self.assertEqual(proxies_t[0]["proxy"], "http://9.9.9.9:80")

    def test_save_group_overwrite(self):
        """同组二次 save_group 替换旧行（不残留）"""
        self.store.save_group("default-http", {"ttl": 3600, "proxies": [
            {"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 0},
            {"proxy": "http://5.6.7.8:3128", "joined_at": time.time(), "consecutive_fails": 0},
        ]})
        self.store.save_group("default-http", {"ttl": 3600, "proxies": [
            {"proxy": "http://9.9.9.9:80", "joined_at": time.time(), "consecutive_fails": 1},
        ]})
        loaded = self.store.load()
        proxies = loaded["proxy_groups"]["default-http"]["proxies"]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0]["proxy"], "http://9.9.9.9:80")

    def test_save_source_paused(self):
        """source_paused 增量（全量替换表）"""
        self.store.save_source_paused({"src.interfaces.sources.ip89": {"paused_at": time.time(), "ttl": 1200}})
        loaded = self.store.load()
        self.assertIn("src.interfaces.sources.ip89", loaded["source_paused"])
        self.store.save_source_paused({"src.interfaces.sources.geonode": {"paused_at": time.time(), "ttl": 600}})
        loaded = self.store.load()
        self.assertNotIn("src.interfaces.sources.ip89", loaded["source_paused"])
        self.assertIn("src.interfaces.sources.geonode", loaded["source_paused"])

    def test_proxy_cross_group(self):
        """代理跨组副本独立存储（复合主键 proxy, group_name）"""
        self.store.save_full({
            "proxy_groups": {
                "default-http": {"ttl": 3600, "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 0}]},
                "trusted": {"ttl": None, "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 0}]},
            },
            "source_paused": {},
        })
        loaded = self.store.load()
        self.assertIn("http://1.2.3.4:8080", [p["proxy"] for p in loaded["proxy_groups"]["default-http"]["proxies"]])
        self.assertIn("http://1.2.3.4:8080", [p["proxy"] for p in loaded["proxy_groups"]["trusted"]["proxies"]])

    def test_wal_not_corrupt_on_crash(self):
        """写入后不 close（模拟异常退出），新连接 load 正常（WAL 自动恢复）"""
        self.store.save_full({
            "proxy_groups": {"default-http": {"ttl": 3600, "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": time.time(), "consecutive_fails": 0}]}},
            "source_paused": {},
        })
        store2 = SqliteProxyStore(self.db_path)
        loaded = store2.load()
        self.assertEqual(loaded["proxy_groups"]["default-http"]["proxies"][0]["proxy"], "http://1.2.3.4:8080")
        store2.close()

    def test_save_full_empty_state(self):
        """save_full 空状态后 load 返回空组结构"""
        self.store.save_full({"proxy_groups": {}, "source_paused": {}})
        self.assertEqual(self.store.load(), {})


class TestSqliteProxyStoreWithPool(unittest.TestCase):
    """与 ProxyPool 集成：save_full → load → restore_state 还原"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, ".cache.db")
        self.store = SqliteProxyStore(self.db_path)

    def tearDown(self):
        self.store.close()
        _cleanup_db(self.db_path)
        os.rmdir(self.tmp_dir)

    def test_save_load_restore_roundtrip(self):
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=1, latency=0.5)
        pool.add_to_group("trusted", "http://9.9.9.9:80")
        pool.pause_source("src.interfaces.sources.ip89")
        self.store.save_full(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertEqual(pool2.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 1)
        self.assertEqual(pool2.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["latency"], 0.5)
        self.assertIn("http://9.9.9.9:80", pool2.proxy_groups["trusted"]["proxies"])
        self.assertIn("src.interfaces.sources.ip89", pool2.source_paused)

    def test_load_filters_expired(self):
        pool = make_pool()
        pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"] = {
            "joined_at": time.time() - PROXY_GROUPS["default-http"] - 1, "consecutive_fails": 0
        }
        pool.add_to_group("trusted", "http://9.9.9.9:80")
        self.store.save_full(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertNotIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://9.9.9.9:80", pool2.proxy_groups["trusted"]["proxies"])

    def test_save_group_with_pool_export_group(self):
        """增量保存：save_group + export_group 还原"""
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.store.save_full(pool.export_state())
        pool.clear_dirty()

        pool.add_to_group("default-http", "http://5.6.7.8:3128")
        for g in pool.dirty_groups():
            self.store.save_group(g, pool.export_group(g))
        pool.clear_dirty()

        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://5.6.7.8:3128", pool2.proxy_groups["default-http"]["proxies"])

    def test_save_load_round_trip_many(self):
        pool = make_pool()
        for i in range(50):
            pool.add_to_group("default-http", f"http://{i}.0.0.0:8080", consecutive_fails=i % 3)
        self.store.save_full(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertEqual(len(pool2.proxy_groups["default-http"]["proxies"]), 50)

    def test_source_pause_persisted(self):
        pool = make_pool()
        pool.pause_source("src.interfaces.sources.ip89")
        self.store.save_full(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertIn("src.interfaces.sources.ip89", pool2.source_paused)

    def test_source_pause_expired_not_persisted(self):
        pool = make_pool()
        pool.source_paused["src.interfaces.sources.ip89"] = {
            "paused_at": time.time() - SOURCE_PAUSE_TTL - 1, "ttl": None
        }
        self.store.save_full(pool.export_state())
        pool2 = make_pool()
        pool2.restore_state(self.store.load())
        self.assertNotIn("src.interfaces.sources.ip89", pool2.source_paused)

    # ---------------- IP 纯洁度缓存 ----------------
    def test_purity_cache_save_and_get(self):
        """写入后读取返回相同结果 + cached_at 时间戳"""
        result = {"abuse_confidence_score": 3, "usage_type": "Data Center/Web Hosting/Transit"}
        self.store.save_purity("1.2.3.4", result)
        cached = self.store.get_purity("1.2.3.4")
        assert cached is not None
        assert cached["result"]["abuse_confidence_score"] == 3
        assert cached["result"]["usage_type"] == "Data Center/Web Hosting/Transit"
        assert cached["cached_at"] > 0

    def test_purity_cache_miss_returns_none(self):
        """未缓存的 IP 返回 None"""
        assert self.store.get_purity("9.9.9.9") is None

    def test_purity_cache_upsert(self):
        """同 IP 二次写入覆盖旧值"""
        self.store.save_purity("1.2.3.4", {"abuse_confidence_score": 0})
        self.store.save_purity("1.2.3.4", {"abuse_confidence_score": 100})
        cached = self.store.get_purity("1.2.3.4")
        assert cached["result"]["abuse_confidence_score"] == 100

    def test_purity_cache_survives_reopen(self):
        """缓存持久化：关闭并重开数据库后缓存仍在"""
        result = {"abuse_confidence_score": 5, "usage_type": "Fixed Line ISP"}
        self.store.save_purity("5.6.7.8", result)
        self.store.close()
        self.store = SqliteProxyStore(self.db_path)
        cached = self.store.get_purity("5.6.7.8")
        assert cached is not None
        assert cached["result"]["abuse_confidence_score"] == 5


class TestGeoCache(unittest.TestCase):
    """IP 归属缓存 (ip_geo_cache) 批量读写"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, ".cache.db")
        self.store = SqliteProxyStore(self.db_path)

    def tearDown(self):
        self.store.close()
        _cleanup_db(self.db_path)
        os.rmdir(self.tmp_dir)

    def test_get_geo_batch_empty(self):
        assert self.store.get_geo_batch([]) == {}

    def test_get_geo_batch_miss_all(self):
        assert self.store.get_geo_batch(["1.2.3.4"]) == {}

    def test_save_then_get(self):
        self.store.save_geo_batch({"1.2.3.4": {"country": "中国", "country_code": "CN", "province": "上海",
                                                "city": "上海", "lat": 31.23, "lng": 121.47}})
        result = self.store.get_geo_batch(["1.2.3.4"])
        assert "1.2.3.4" in result
        assert result["1.2.3.4"]["country"] == "中国"
        assert result["1.2.3.4"]["lat"] == 31.23
        assert result["1.2.3.4"]["cached_at"] > 0

    def test_batch_save_and_batch_get(self):
        self.store.save_geo_batch({
            "1.1.1.1": {"country": "A", "country_code": "AA", "lat": 1.0, "lng": 1.0},
            "2.2.2.2": {"country": "B", "country_code": "BB", "lat": 2.0, "lng": 2.0},
        })
        result = self.store.get_geo_batch(["1.1.1.1", "2.2.2.2", "3.3.3.3"])
        assert len(result) == 2
        assert "3.3.3.3" not in result

    def test_upsert_overwrites(self):
        self.store.save_geo_batch({"1.2.3.4": {"country": "旧", "country_code": "XX", "lat": 0.0, "lng": 0.0}})
        self.store.save_geo_batch({"1.2.3.4": {"country": "新", "country_code": "YY", "lat": 1.0, "lng": 1.0}})
        result = self.store.get_geo_batch(["1.2.3.4"])
        assert result["1.2.3.4"]["country"] == "新"


if __name__ == "__main__":
    unittest.main()
