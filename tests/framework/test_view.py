"""view.py 测试：缓存解析纯函数单测 + 无 mainloop 的 GUI 冒烟

- 纯函数部分覆盖加载、失效判定（过期/超限/永不过期/trusted 阈值）、排序、汇总、源暂停
- GUI 冒烟：用临时 .cache 实例化 CacheViewer 后 refresh，断言各 Treeview 行数与着色 tag；
  无显示环境（TclError）时自动跳过
"""

import logging
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

import view
from view import (
    CacheViewer, build_group_summary, build_proxy_rows, build_source_rows,
    format_duration, format_remaining, format_ttl,
    load_cache, max_consecutive_fails_of, resolve_group_ttl,
)
from src.framework import config
from src.interfaces.sqlite_store import SqliteProxyStore


def _cleanup_db(path: str) -> None:
    """删除 SQLite 数据库及其 WAL/SHM 侧车文件"""
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


NOW = 1_800_000_000
DEFAULT_TTL = 240


def make_sample(now):
    """样例缓存（相对传入 now 生成，单测用固定 NOW，GUI 冒烟用真实时间）

    覆盖：过期 / 超限 / 存活 / 即将过期 / 永不过期（trusted）、源暂停、缺字段容错。
    连续失败数按配置阈值动态生成（达到阈值超限 / 阈值-1 存活），与具体配置值无关。
    """
    return {
        "proxy_groups": {
            "default-http": {
                "ttl": DEFAULT_TTL,
                "proxies": [
                    {"proxy": "http://1.1.1.1:8080", "joined_at": now - 300, "consecutive_fails": 0},   # 已过期
                    {"proxy": "http://2.2.2.2:8080", "joined_at": now - 100, "consecutive_fails": config.MAX_CONSECUTIVE_FAILS},   # 超限（达到阈值）
                    {"proxy": "http://3.3.3.3:8080", "joined_at": now - 200, "consecutive_fails": 0},   # 即将过期（剩 40s）
                    {"proxy": "http://4.4.4.4:8080", "joined_at": now - 10, "consecutive_fails": 0},    # 存活
                    {"proxy": "http://5.5.5.5:8080", "joined_at": now - 10, "extra": True}, # 缺字段容错
                ],
            },
            "trusted": {
                "ttl": None,
                "proxies": [
                    {"proxy": "http://9.9.9.9:80", "joined_at": 0, "consecutive_fails": config.TRUSTED_MAX_CONSECUTIVE_FAILS},             # trusted 超限（达到阈值）
                    {"proxy": "http://8.8.8.8:80", "joined_at": 0, "consecutive_fails": config.TRUSTED_MAX_CONSECUTIVE_FAILS - 1},         # trusted 存活（阈值-1）
                ],
            },
        },
        "source_paused": {
            "src.interfaces.sources.ip89": {"paused_at": now - 100, "ttl": 300},            # 存续
            "src.interfaces.sources.goodips": {"paused_at": now - 500, "ttl": 300},         # 过期残留
            "src.interfaces.sources.geonode": {"paused_at": now - 100},                     # ttl 缺省 -> 默认 1200
        },
    }


SAMPLE = make_sample(NOW)


class TestLoadCache(unittest.TestCase):
    def test_valid_file(self):
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, ".cache.db")
        # 构造 SqliteProxyStore 能完整往返的 state（只含 schema 字段，不含 extra 等非持久化字段）
        state = {
            "proxy_groups": {
                "default-http": {"ttl": 240, "proxies": [
                    {"proxy": "http://1.1.1.1:8080", "joined_at": NOW - 300, "consecutive_fails": 0},
                    {"proxy": "http://2.2.2.2:8080", "joined_at": NOW - 100, "consecutive_fails": 2},
                ]},
                "trusted": {"ttl": None, "proxies": [
                    {"proxy": "http://9.9.9.9:80", "joined_at": 0, "consecutive_fails": 3},
                ]},
            },
            "source_paused": {"src.interfaces.sources.ip89": {"paused_at": NOW - 100, "ttl": 300}},
        }
        try:
            store = SqliteProxyStore(db_path)
            store.save_full(state)
            store.close()
            self.assertEqual(load_cache(db_path), state)
        finally:
            _cleanup_db(db_path)
            os.rmdir(tmp_dir)

    def test_missing_file(self):
        self.assertEqual(load_cache(os.path.join(tempfile.gettempdir(), "no_such_cache_xxx")), {})

    def test_corrupt_file(self):
        """非 SQLite 格式文件 load_cache 返回空 dict（容错）"""
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, ".cache.db")
        try:
            with open(db_path, "wb") as f:
                f.write(b"not a sqlite database")
            self.assertEqual(load_cache(db_path), {})
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_default_path_is_project_root(self):
        # 与 view.default_cache_path() 实现同源（view.py 所在目录的 .cache.db）
        expected = os.path.join(os.path.dirname(os.path.abspath(view.__file__)), ".cache.db")
        self.assertEqual(view.default_cache_path(), expected)


class TestBuildProxyRows(unittest.TestCase):
    def setUp(self):
        self.rows = build_proxy_rows(SAMPLE, now=NOW)
        self.by_proxy = {r["proxy"]: r for r in self.rows}

    def test_status_classification(self):
        self.assertEqual(self.by_proxy["http://1.1.1.1:8080"]["status"], "expired")
        self.assertEqual(self.by_proxy["http://2.2.2.2:8080"]["status"], "invalid")
        self.assertEqual(self.by_proxy["http://4.4.4.4:8080"]["status"], "alive")
        # trusted 组用 trusted 阈值：达到阈值超限 / 阈值-1 存活
        self.assertEqual(self.by_proxy["http://9.9.9.9:80"]["status"], "invalid")
        self.assertEqual(self.by_proxy["http://8.8.8.8:80"]["status"], "alive")

    def test_remains_ttl_none_never_expire(self):
        self.assertEqual(self.by_proxy["http://8.8.8.8:80"]["remaining"], None)
        self.assertEqual(self.by_proxy["http://9.9.9.9:80"]["remaining"], None)

    def test_missing_fields_tolerated(self):
        self.assertIn("http://5.5.5.5:8080", self.by_proxy)
        self.assertEqual(self.by_proxy["http://5.5.5.5:8080"]["consecutive_fails"], 0)

    def test_sort_order(self):
        # 排序：已过期在前，其余按剩余时间升序（永不过期最后）
        orders = [r["status"] for r in self.rows]
        expired = [i for i, s in enumerate(orders) if s == "expired"]
        invalid = [i for i, s in enumerate(orders) if s == "invalid"]
        alive = [i for i, s in enumerate(orders) if s == "alive"]
        self.assertTrue(all(e < min(invalid + alive) for e in expired))
        for i in range(1, len(self.rows)):
            prev, cur = self.rows[i - 1], self.rows[i]
            if prev["status"] == cur["status"]:
                # 同状态（expired/alive）剩余时间升序；None 视为最大
                prev_rem = prev["remaining"] if prev["remaining"] is not None else (1 << 62)
                cur_rem = cur["remaining"] if cur["remaining"] is not None else (1 << 62)
                self.assertLessEqual(prev_rem, cur_rem)


class TestThresholds(unittest.TestCase):
    def test_max_consecutive_fails_of(self):
        self.assertEqual(max_consecutive_fails_of("trusted"), config.TRUSTED_MAX_CONSECUTIVE_FAILS)
        self.assertEqual(max_consecutive_fails_of("default-http"), config.MAX_CONSECUTIVE_FAILS)
        self.assertEqual(max_consecutive_fails_of("freeproxy-world-http"), config.MAX_CONSECUTIVE_FAILS)

    def test_group_ttl_prefers_cache(self):
        cache = {"proxy_groups": {"default-http": {"ttl": 123, "proxies": []}}}
        self.assertEqual(resolve_group_ttl(cache, "default-http"), 123)
        # 组不在缓存时回退配置
        self.assertEqual(resolve_group_ttl(SAMPLE, "blacklist"), config.PROXY_GROUPS["blacklist"])


class TestGroupSummary(unittest.TestCase):
    def test_summary_counts(self):
        summary = {s["group"]: s for s in build_group_summary(SAMPLE, build_proxy_rows(SAMPLE, now=NOW))}
        self.assertEqual(summary["default-http"]["total"], 5)
        self.assertEqual(summary["default-http"]["expired"], 1)
        self.assertEqual(summary["default-http"]["invalid"], 1)
        self.assertEqual(summary["default-http"]["alive"], 3)
        self.assertEqual(summary["trusted"]["ttl"], None)
        self.assertEqual(summary["trusted"]["invalid"], 1)
        # 组顺序：配置 PROXY_GROUPS 在前
        groups = [s["group"] for s in build_group_summary(SAMPLE, build_proxy_rows(SAMPLE, now=NOW))]
        self.assertEqual(groups[7], "trusted")
        self.assertEqual(groups[8], "blacklist")


class TestSourceRows(unittest.TestCase):
    def test_rows(self):
        rows = {r["source"]: r for r in build_source_rows(SAMPLE, now=NOW)}
        self.assertTrue(rows["src.interfaces.sources.ip89"]["active"])
        self.assertFalse(rows["src.interfaces.sources.goodips"]["active"])
        self.assertEqual(rows["src.interfaces.sources.geonode"]["ttl"], config.SOURCE_PAUSE_TTL)
        self.assertEqual(rows["src.interfaces.sources.goodips"]["remaining"], -200)


class TestFormatting(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(format_duration(25), "25秒")
        self.assertEqual(format_duration(205), "3分25秒")
        self.assertEqual(format_duration(3720), "1时2分")
        self.assertEqual(format_duration(90000), "1天1时")

    def test_ttl(self):
        self.assertEqual(format_ttl(None), "永不过期")
        self.assertEqual(format_ttl(240), "4分0秒")

    def test_remaining(self):
        row = {"ttl": None, "remaining": None}
        self.assertEqual(format_remaining(row), "永不过期")
        row = {"ttl": 240, "remaining": -5}
        self.assertEqual(format_remaining(row), "已过期")
        row = {"ttl": 240, "remaining": 42}
        self.assertEqual(format_remaining(row), "剩 42秒")


class TestGuiSmoke(unittest.TestCase):
    """无 mainloop 实例化并刷新，断言表格内容与着色 tag"""

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp()
        cls.cache_path = os.path.join(cls.tmp_dir, ".cache.db")
        store = SqliteProxyStore(cls.cache_path)
        store.save_full(make_sample(time.time()))
        store.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_db(cls.cache_path)
        os.rmdir(cls.tmp_dir)

    def _rows_with_tag(self, tree, tag):
        return tree.tag_has(tag)

    def test_viewer_refresh(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:  # 无显示环境（headless/CI）
            self.skipTest(f"tkinter 不可用: {exc}")
        try:
            viewer = CacheViewer(root, self.cache_path)
            root.update_idletasks()

            # 总代理数 = 7（default 5 + trusted 2）
            self.assertEqual(len(viewer.all_tree.get_children()), 7)

            # 组标签：default + trusted（+ blacklist 配置组也应有标签）
            self.assertIn("default-http", viewer.group_tabs)
            self.assertIn("trusted", viewer.group_tabs)

            # 各状态着色
            expired = viewer.all_tree.tag_has("expired")
            invalid = viewer.all_tree.tag_has("invalid")
            near = viewer.all_tree.tag_has("near")
            self.assertEqual(len(expired), 1)
            self.assertEqual(len(invalid), 2)  # default 1 + trusted 1
            self.assertEqual(len(near), 1)

            # 概览表
            overview_rows = viewer.overview_tree.get_children()
            self.assertEqual(len(overview_rows), len(config.PROXY_GROUPS))  # 配置的组数
            values = [viewer.overview_tree.item(i, "values")[1] for i in overview_rows]
            self.assertIn("永不过期", values)

            # 源暂停表：3 条记录（含过期残留）
            self.assertEqual(len(viewer.source_tree.get_children()), 3)

            # 过滤：仅保留 1.1.1.1
            viewer.filter_var.set("1.1.1.1")
            root.update_idletasks()
            self.assertEqual(len(viewer.all_tree.get_children()), 1)

            viewer.auto_refresh_var.set(False)
            viewer.root.destroy()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()