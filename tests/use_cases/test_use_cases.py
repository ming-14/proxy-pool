"""用例层测试（原 test_proxy_manager.py 33 例 + scheduler 函数用例 9 例）"""

import asyncio
import importlib
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.models import SourceSpec
from src.domain.proxy_pool import ProxyPool
from src.interfaces.sqlite_store import SqliteProxyStore
from src.interfaces.scheduler import ProxyScheduler
from src.interfaces.proxy_scheme import verify_target
from src.use_cases.fetch_proxies import FetchProxiesUseCase
from src.use_cases.get_proxies import GetProxiesUseCase
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.request_with_retry import RequestWithRetryUseCase
from src.use_cases.check_proxies import CheckProxiesUseCase
from src.framework.config import (
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    PROXY_GROUPS,
    SOURCES as _SOURCES,
    SOURCE_DEDUP_THRESHOLD,
    FETCH_PROXY_RETRIES,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
    STANDBY_GROUP,
)

SOURCES = [{**s, "enabled": True} for s in _SOURCES]
SOURCE_SPECS = [SourceSpec(**s) for s in SOURCES]

# freeproxy-world 与 proxy-scdn 源在 config.SOURCES 中的实际位置（源名由 config 决定）
FPW_SOURCE = SOURCES[4]["name"]
SCDN_SOURCE = SOURCES[0]["name"]


def make_pool():
    """构造默认配置的 ProxyPool（等价组合根的行为）"""
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "freeproxy-world-http", "freeproxy-world-https", "freeproxy-world-socks5", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


def default_loader(name):
    """真实加载源实例（组合根同款实现：模块暴露 create_source 工厂）"""
    return importlib.import_module(name).create_source()


def make_fetch(pool, loader=None):
    return FetchProxiesUseCase(
        pool=pool,
        sources=SOURCE_SPECS,
        loader=loader or default_loader,
        fetch_retries=FETCH_PROXY_RETRIES,
        dedup_threshold=SOURCE_DEDUP_THRESHOLD,
    )


def mock_proxy_response(proxies):
    """构造 proxy-scdn 格式的 mock 响应"""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": 200, "data": {"proxies": proxies}}
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


class TestRecordProxyUseCase(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()
        self.record = RecordProxyUseCase(pool=self.pool, standby_group=STANDBY_GROUP)

    def test_empty_cache(self):
        for name in PROXY_GROUPS:
            self.assertEqual(len(self.pool.proxy_groups[name]["proxies"]), 0)

    def test_record_success_moves_to_trusted(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.record.record_success("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])

    def test_record_success_already_trusted(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=1)
        self.record.record_success("http://1.2.3.4:8080")
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 1)

    def test_record_success_resets_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=1)
        self.record.record_success("http://1.2.3.4:8080")
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 0)

    def test_record_success_standby_copies_not_moves(self):
        """代理服务队列成员成功后复制进 trusted 并清零队列副本连续失败计数，保留在队列（serve 上游不被掏空）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", consecutive_fails=1)
        self.record.record_success("http://1.2.3.4:8080")
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups[STANDBY_GROUP]["proxies"])
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        # 连续语义：成功清零队列副本的连续失败计数（不保留旧累计值）
        self.assertEqual(self.pool.proxy_groups[STANDBY_GROUP]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 0)

    def test_remove_clears_all_copies(self):
        """拉黑时清理所有组副本（standby 复制进 trusted 的残留副本一并清除）"""
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.record.remove("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups[STANDBY_GROUP]["proxies"])
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_available_dedups_multi_group_copies(self):
        """同代理多组共存时 get_available_proxies 只返回一次（复制语义下不会重复返回）"""
        pool = make_pool()
        pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080")
        pool.add_to_group("trusted", "http://1.2.3.4:8080")
        available = pool.get_available_proxies()
        self.assertEqual(sum(1 for p in available if p == "http://1.2.3.4:8080"), 1)

    def test_record_fail(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.record.record_fail("http://1.2.3.4:8080")
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 1)

    def test_record_fail_trusted(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.record.record_fail("http://1.2.3.4:8080")
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 1)

    def test_record_fail_nonexistent(self):
        self.record.record_fail("http://0.0.0.0:0")

    def test_remove_moves_to_blacklist(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.record.remove("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_remove_trusted(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.record.remove("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_remove_nonexistent(self):
        self.record.remove("http://0.0.0.0:0")
        self.assertTrue(self.pool.is_blacklisted("http://0.0.0.0:0"))

    def test_trust(self):
        self.record.trust("http://1.2.3.4:8080")
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])

    def test_auto_remove_on_max_consecutive_fails(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS - 1)
        self.record.record_fail("http://1.2.3.4:8080")
        self.record.remove("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])

    def test_trusted_removed_on_max_consecutive_fails(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS - 1)
        self.record.record_fail("http://1.2.3.4:8080")
        self.record.remove("http://1.2.3.4:8080")
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])

    def test_get_trusted_proxies(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        self.pool.add_to_group("trusted", "http://5.6.7.8:3128", consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS)
        trusted = self.pool.get_trusted_proxies()
        self.assertIn("http://1.2.3.4:8080", trusted)
        self.assertNotIn("http://5.6.7.8:3128", trusted)

    def test_get_consecutive_fail_count(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=2)
        self.assertEqual(self.record.get_consecutive_fail_count("http://1.2.3.4:8080"), 2)
        self.assertEqual(self.record.get_consecutive_fail_count("http://0.0.0.0:0"), 0)


class TestGetProxiesUseCase(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()

    def test_get_proxies_returns_available(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128", consecutive_fails=MAX_CONSECUTIVE_FAILS)
        get = GetProxiesUseCase(pool=self.pool)
        proxies = get.execute(1)
        self.assertEqual(proxies, ["http://1.2.3.4:8080"])

    def test_get_proxies_none_when_all_exhausted(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=MAX_CONSECUTIVE_FAILS)
        get = GetProxiesUseCase(pool=self.pool)
        proxies = get.execute(1)
        self.assertEqual(proxies, [])

    def test_get_proxies_with_exclude(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        get = GetProxiesUseCase(pool=self.pool)
        proxies = get.execute(1, exclude={"http://1.2.3.4:8080"})
        self.assertEqual(proxies, ["http://5.6.7.8:3128"])

    def test_get_proxies_auto_fetch_on_empty(self):
        fetch = MagicMock()
        get = GetProxiesUseCase(pool=self.pool, fetch=fetch)
        get.execute(1)
        fetch.assert_called_once()

    def test_get_proxies_no_auto_fetch_when_available(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        fetch = MagicMock()
        get = GetProxiesUseCase(pool=self.pool, fetch=fetch)
        get.execute(1)
        fetch.assert_not_called()

    def test_get_proxies_returns_at_most_n(self):
        for i in range(5):
            self.pool.add_to_group("default-http", f"http://{i}.0.0.0:8080")
        get = GetProxiesUseCase(pool=self.pool)
        proxies = get.execute(3)
        self.assertEqual(len(proxies), 3)

    def test_get_proxies_returns_all_if_n_exceeds(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.pool.add_to_group("default-http", "http://5.6.7.8:3128")
        get = GetProxiesUseCase(pool=self.pool)
        proxies = get.execute(10)
        self.assertEqual(len(proxies), 2)


class TestFetchProxiesUseCase(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()

    def test_fetch_proxies_adds_to_correct_group(self):
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://5.6.7.8:3128"])):
            make_fetch(self.pool).execute()
        self.assertIn("http://5.6.7.8:3128", self.pool.proxy_groups["default-http"]["proxies"])

    def test_fetch_single_source_only_loads_that_source(self):
        """单源拉取：只加载并调用指定源（面板单源拉取按钮）"""
        loaded = []

        def loader(name):
            loaded.append(name)
            src = MagicMock()
            src.fetch_proxies.return_value = ["http://5.6.7.8:3128"] if name == SCDN_SOURCE else []
            return src

        make_fetch(self.pool, loader=loader).execute(SCDN_SOURCE)
        self.assertEqual(loaded, [SCDN_SOURCE])
        self.assertIn("http://5.6.7.8:3128", self.pool.proxy_groups["default-http"]["proxies"])

    def test_fetch_single_source_unknown_raises(self):
        """未知源：抛 ValueError（调用方传错名字，不能与「暂停中所以无事可做」混为一谈）"""
        loaded = []
        with self.assertRaises(ValueError):
            make_fetch(self.pool, loader=lambda name: loaded.append(name)).execute("src.interfaces.sources.not_exist")
        self.assertEqual(loaded, [])
        self.assertEqual(len(self.pool.proxy_groups["default-http"]["proxies"]), 0)

    def test_fetch_single_source_skips_paused(self):
        """已暂停的源即使被指定也不拉取（暂停态下前端不显示该按钮）"""
        self.pool.pause_source(SCDN_SOURCE, ttl=60)
        loaded = []
        make_fetch(self.pool, loader=lambda name: loaded.append(name)).execute(SCDN_SOURCE)
        self.assertEqual(loaded, [])

    def test_fetch_proxies_no_duplicate(self):
        self.pool.add_to_group("default-http", "http://1.2.3.4:8080", consecutive_fails=1)
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://1.2.3.4:8080", "http://5.6.7.8:3128"])):
            make_fetch(self.pool).execute()
        self.assertEqual(self.pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 1)

    def test_fetch_proxies_skips_blacklisted(self):
        self.pool.add_to_group("blacklist", "http://1.2.3.4:8080")
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://1.2.3.4:8080", "http://5.6.7.8:3128"])):
            make_fetch(self.pool).execute()
        self.assertNotIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])

    def test_freeproxy_world_paused_after_fetch(self):
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://5.6.7.8:3128"])):
            make_fetch(self.pool).execute()
        self.assertIsNotNone(self.pool.source_pause_remaining(FPW_SOURCE))
        self.assertEqual(self.pool.source_paused[FPW_SOURCE]["ttl"], SOURCES[4]["pause_ttl"])

    def test_freeproxy_world_paused_even_on_failure(self):
        with patch("src.interfaces.http_client.get", side_effect=Exception("fail")):
            make_fetch(self.pool).execute()
        self.assertIsNotNone(self.pool.source_pause_remaining(FPW_SOURCE))

    def test_freeproxy_world_proxies_in_own_group(self):
        mock_mod = MagicMock()
        mock_mod.create_source.return_value.fetch_proxies.return_value = ["http://1.2.3.4:8080"]

        def loader(name):
            if name == FPW_SOURCE:
                return mock_mod.create_source()
            return default_loader(name)

        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://5.6.7.8:3128"])):
            make_fetch(self.pool, loader=loader).execute()
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["freeproxy-world-http"]["proxies"])

    def test_source_paused_on_high_dedup(self):
        for i in range(95):
            self.pool.add_to_group("trusted", f"http://{i}.0.0.0:8080")
        self.pool.add_to_group("blacklist", "http://95.0.0.0:8080")
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response([f"http://{i}.0.0.0:8080" for i in range(100)])):
            make_fetch(self.pool).execute()
        self.assertIn(SCDN_SOURCE, self.pool.source_paused)

    def test_source_not_paused_below_threshold(self):
        for i in range(50):
            self.pool.add_to_group("trusted", f"http://{i}.0.0.0:8080")
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response([f"http://{i}.0.0.0:8080" for i in range(100)])):
            make_fetch(self.pool).execute()
        dedup_paused = [s for s in self.pool.source_paused if self.pool.source_paused[s].get("ttl") != SOURCES[4]["pause_ttl"]]
        self.assertEqual(len(dedup_paused), 0)

    def test_fetch_proxies_skips_paused_source(self):
        self.pool.pause_source(SCDN_SOURCE)
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://5.6.7.8:3128"])):
            make_fetch(self.pool).execute()
        self.assertNotIn("http://5.6.7.8:3128", self.pool.proxy_groups["default-http"]["proxies"])

    def test_fetch_proxies_dedup_across_sources(self):
        with patch("src.interfaces.http_client.get", return_value=mock_proxy_response(["http://1.2.3.4:8080"])):
            make_fetch(self.pool).execute()
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["default-http"]["proxies"])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, ".cache.db")

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            p = self.db_path + ext
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(self.tmp_dir)

    def test_trusted_survives_default_expiry(self):
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080")
        pool.add_to_group("trusted", "http://9.9.9.9:80")
        pool.proxy_groups["default-http"]["proxies"]["http://1.2.3.4:8080"]["joined_at"] = time.time() - PROXY_GROUPS["default-http"] - 1
        store = SqliteProxyStore(self.db_path)
        store.save_full(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(store.load())
        store.close()
        self.assertNotIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])
        self.assertIn("http://9.9.9.9:80", pool2.proxy_groups["trusted"]["proxies"])

    def test_save_cache_persists(self):
        pool = make_pool()
        pool.add_to_group("default-http", "http://1.2.3.4:8080")
        store = SqliteProxyStore(self.db_path)
        store.save_full(pool.export_state())

        pool2 = make_pool()
        pool2.restore_state(store.load())
        store.close()
        self.assertIn("http://1.2.3.4:8080", pool2.proxy_groups["default-http"]["proxies"])


class TestRequestWithRetry(unittest.TestCase):
    def test_success_on_first_try(self):
        get = MagicMock()
        get.execute.return_value = ["http://1.1.1.1:8080"]
        record = MagicMock()
        http_getter = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        http_getter.get.return_value = mock_resp
        uc = RequestWithRetryUseCase(get_proxies=get, record=record, http_getter=http_getter)
        proxy, resp = uc.execute("http://example.com")
        self.assertEqual(proxy, "http://1.1.1.1:8080")
        record.record_success.assert_called_once_with("http://1.1.1.1:8080")

    def test_retry_on_failure(self):
        get = MagicMock()
        get.execute.side_effect = [["http://1.1.1.1:8080"], ["http://2.2.2.2:3128"]]
        record = MagicMock()
        record.should_blacklist.return_value = False
        http_getter = MagicMock()
        mock_resp = MagicMock()
        http_getter.get.side_effect = [Exception("fail"), mock_resp]
        uc = RequestWithRetryUseCase(get_proxies=get, record=record, http_getter=http_getter)
        proxy, resp = uc.execute("http://example.com", max_retries=3)
        self.assertEqual(proxy, "http://2.2.2.2:3128")
        record.record_fail.assert_called()

    def test_all_retries_exhausted(self):
        get = MagicMock()
        get.execute.side_effect = [["http://1.1.1.1:8080"], ["http://2.2.2.2:3128"], []]
        record = MagicMock()
        record.should_blacklist.return_value = False
        http_getter = MagicMock()
        http_getter.get.side_effect = Exception("fail")
        uc = RequestWithRetryUseCase(get_proxies=get, record=record, http_getter=http_getter)
        with self.assertRaises(Exception):
            uc.execute("http://example.com", max_retries=3)

    def test_no_proxies_available(self):
        get = MagicMock()
        get.execute.return_value = []
        record = MagicMock()
        http_getter = MagicMock()
        uc = RequestWithRetryUseCase(get_proxies=get, record=record, http_getter=http_getter)
        with self.assertRaises(RuntimeError):
            uc.execute("http://example.com")

    def test_blacklist_on_max_consecutive_fails(self):
        get = MagicMock()
        get.execute.side_effect = [["http://1.1.1.1:8080"], ["http://2.2.2.2:3128"]]
        record = MagicMock()
        record.should_blacklist.return_value = True
        http_getter = MagicMock()
        mock_resp = MagicMock()
        http_getter.get.side_effect = [Exception("fail"), mock_resp]
        uc = RequestWithRetryUseCase(get_proxies=get, record=record, http_getter=http_getter)
        uc.execute("http://example.com", max_retries=3)
        record.remove.assert_called_with("http://1.1.1.1:8080")


class TestCheckProxiesConcurrent(unittest.TestCase):
    def make_uc(self, async_getter):
        return CheckProxiesUseCase(
            scheduler_factory=lambda proxies, **kw: ProxyScheduler(proxies, **kw),
            async_getter=async_getter,
            verify_policy=verify_target,
        )

    def make_async_getter(self, *, status_code=200, exc=None):
        """创建 async_getter mock：返回 status_code 或抛 exc"""
        getter = MagicMock()

        async def get(url, *, timeout, headers=None, proxy=None, verify=False):
            if exc is not None:
                raise exc
            return MagicMock(status_code=status_code)

        getter.get.side_effect = get
        return getter

    def test_all_succeed(self):
        getter = self.make_async_getter(status_code=200)
        results = asyncio.run(self.make_uc(getter).execute(
            ["http://1.1.1.1:8080", "http://2.2.2.2:3128"], "http://example.com", max_concurrent=2,
        ))
        self.assertTrue(all(r["success"] for r in results.values()))
        self.assertEqual(len(results), 2)

    def test_all_fail(self):
        getter = self.make_async_getter(exc=Exception("fail"))
        results = asyncio.run(self.make_uc(getter).execute(
            ["http://1.1.1.1:8080"], "http://example.com", max_concurrent=1,
        ))
        self.assertFalse(results["http://1.1.1.1:8080"]["success"])
        self.assertIn("fail", results["http://1.1.1.1:8080"]["error"])

    def test_mixed_results(self):
        getter = MagicMock()
        call_count = 0

        async def side_effect(url, *, timeout, headers=None, proxy=None, verify=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("fail")
            return MagicMock(status_code=200)

        getter.get.side_effect = side_effect
        results = asyncio.run(self.make_uc(getter).execute(
            ["http://1.1.1.1:8080", "http://2.2.2.2:3128"], "http://example.com", max_concurrent=2,
        ))
        successes = sum(1 for r in results.values() if r["success"])
        self.assertEqual(successes, 1)

    def test_empty_proxies(self):
        getter = self.make_async_getter()
        results = asyncio.run(self.make_uc(getter).execute([], "http://example.com"))
        self.assertEqual(results, {})

    def test_verify_policy_by_proxy_type(self):
        """http/socks5 代理严格校验（verify=True），https 代理不校验（TLS 直连代理自签证书会误杀）"""
        getter = MagicMock()

        async def get(url, *, timeout, headers=None, proxy=None, verify=False):
            return MagicMock(status_code=200)

        getter.get.side_effect = get
        asyncio.run(self.make_uc(getter).execute(
            ["http://1.1.1.1:8080", "socks5://2.2.2.2:1080", "https://3.3.3.3:443"],
            "https://example.com", max_concurrent=3,
        ))
        calls = getter.get.call_args_list
        self.assertEqual(
            {call.kwargs["proxy"]: call.kwargs["verify"] for call in calls},
            {"http://1.1.1.1:8080": True, "socks5://2.2.2.2:1080": True, "https://3.3.3.3:443": False},
        )

    def test_non_2xx_is_failure(self):
        """探测目标返回非 2xx（如 503）判失败：目标站挂了不能说明代理可用"""
        getter = self.make_async_getter(status_code=503)
        results = asyncio.run(self.make_uc(getter).execute(
            ["http://1.1.1.1:8080"], "https://example.com", max_concurrent=1,
        ))
        r = results["http://1.1.1.1:8080"]
        self.assertFalse(r["success"])
        self.assertEqual(r["status_code"], 503)
        self.assertIn("503", r["error"])


if __name__ == "__main__":
    unittest.main()
