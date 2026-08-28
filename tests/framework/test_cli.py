"""CLI 测试（patch cli._get_container 注入 fake container）

保持原 test_cli.py 的 57 个用例语义：
- 经组合根容器取依赖（替代旧的 patch("cli.CacheManager"/"cli.ProxyManager")）
- 单代理 test 仍 patch cli.requests.get
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.framework import cli
from src.framework.container import Container
from src.domain.proxy_pool import ProxyPool
from src.interfaces.sqlite_store import SqliteProxyStore
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework.config import (
    MAX_CONSECUTIVE_FAILS,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL,
    PROXY_GROUPS,
    TRUSTED_GROUP,
    BLACKLIST_GROUP,
    STANDBY_GROUP,
)


def make_pool(db_path):
    pool = ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "freeproxy-world-http", "freeproxy-world-https", "freeproxy-world-socks5", "trusted"),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )
    store = SqliteProxyStore(db_path)
    pool.restore_state(store.load())
    store.close()
    return pool


def _incremental_save(store, pool):
    """模拟 Container.save 增量逻辑（只写 dirty 组）"""
    dirty = pool.dirty_groups()
    if dirty:
        for g in dirty:
            store.save_group(g, pool.export_group(g))
    if pool.source_paused_dirty():
        store.save_source_paused(pool.export_source_paused())
    pool.clear_dirty()


def make_container(db_path):
    """构造 fake container：真实 pool/store/record，其余组件 mock"""
    pool = make_pool(db_path)
    store = SqliteProxyStore(db_path)
    c = MagicMock(spec=Container)
    c.pool = pool
    c.store = store
    c.fetch = MagicMock()
    c.get_proxies = MagicMock()
    c.record = RecordProxyUseCase(pool=pool, standby_group=STANDBY_GROUP)
    c.request_with_retry = MagicMock()
    c.check_proxies = MagicMock()
    c.save = MagicMock(wraps=lambda: _incremental_save(store, pool))
    c.save_full = MagicMock(wraps=lambda: store.save_full(pool.export_state()))
    return c


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class ContainerTestCase(unittest.TestCase):
    """带 fake container 的基础测试类"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, ".cache.db")
        self.container = make_container(self.db_path)
        self._patcher = patch.object(cli, "_get_container", return_value=self.container)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            self.container.store.close()
        except Exception:
            pass
        for ext in ("", "-wal", "-shm"):
            p = self.db_path + ext
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(self.tmp_dir)


class TestPrintProxies(unittest.TestCase):
    def test_plain_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies(["http://1.2.3.4:8080", "http://5.6.7.8:3128"])
        lines = buf.getvalue().rstrip().splitlines()
        self.assertEqual(lines, [
            "  #  代理",
            "  -  -------------------",
            "  1  http://1.2.3.4:8080",
            "  2  http://5.6.7.8:3128",
        ])

    def test_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies(["http://1.2.3.4:8080", "http://5.6.7.8:3128"], as_json=True)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, ["http://1.2.3.4:8080", "http://5.6.7.8:3128"])

    def test_empty_plain(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies([])
        self.assertEqual(buf.getvalue(), "")

    def test_empty_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies([], as_json=True)
        self.assertEqual(json.loads(buf.getvalue()), [])


class TestCmdFetch(ContainerTestCase):
    def test_fetch_plain(self):
        args = _Args(json=False)
        self.container.pool.get_available_proxies = MagicMock(return_value={"http://1.2.3.4:8080": {"group": "default-http", "consecutive_fails": 0}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_fetch(args)
        self.assertIn("http://1.2.3.4:8080", buf.getvalue())
        self.container.fetch.execute.assert_called_once()
        self.container.save_full.assert_called_once()

    def test_fetch_json(self):
        args = _Args(json=True)
        self.container.pool.get_available_proxies = MagicMock(return_value={"http://1.2.3.4:8080": {"group": "default-http", "consecutive_fails": 0}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_fetch(args)
        data = json.loads(buf.getvalue())
        self.assertIn("http://1.2.3.4:8080", data)

    def test_fetch_empty(self):
        args = _Args(json=False)
        self.container.pool.get_available_proxies = MagicMock(return_value={})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_fetch(args)
        self.assertEqual(buf.getvalue().strip(), "")

    def test_fetch_saves_cache_on_exception(self):
        args = _Args(json=False)
        self.container.fetch.execute.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            cli.cmd_fetch(args)
        self.container.save_full.assert_called_once()


class TestCmdList(ContainerTestCase):
    def test_list_all(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.container.pool.add_to_group("trusted", "http://9.9.9.9:80")
        args = _Args(group=None, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        lines = buf.getvalue().strip().splitlines()
        # 表头 + 分隔线 + 2 行数据
        self.assertEqual(len(lines), 4)
        self.assertIn("http://1.2.3.4:8080", lines[2])
        self.assertIn("http://9.9.9.9:80", lines[3])

    def test_list_by_group(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.container.pool.add_to_group("trusted", "http://9.9.9.9:80")
        args = _Args(group="trusted", json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        lines = buf.getvalue().strip().splitlines()
        self.assertIn("http://9.9.9.9:80", lines[-1])

    def test_list_json(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        args = _Args(group=None, json=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        data = json.loads(buf.getvalue())
        self.assertIn("http://1.2.3.4:8080", data)

    def test_list_empty(self):
        args = _Args(group=None, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        self.assertEqual(buf.getvalue(), "")

    def test_ls_alias(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        args = _Args(group=None, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        self.assertIn("http://1.2.3.4:8080", buf.getvalue())


class TestCmdGet(ContainerTestCase):
    def test_get_one(self):
        self.container.get_proxies.execute.return_value = ["http://1.2.3.4:8080"]
        args = _Args(n=1, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_get(args)
        lines = buf.getvalue().strip().splitlines()
        self.assertIn("http://1.2.3.4:8080", lines[-1])
        self.container.get_proxies.execute.assert_called_once_with(1)

    def test_get_multiple(self):
        self.container.get_proxies.execute.return_value = ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]
        args = _Args(n=2, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_get(args)
        lines = buf.getvalue().strip().splitlines()
        # 表头 + 分隔线 + 2 行数据
        self.assertEqual(len(lines), 4)
        self.container.get_proxies.execute.assert_called_once_with(2)

    def test_get_json(self):
        self.container.get_proxies.execute.return_value = ["http://1.2.3.4:8080"]
        args = _Args(n=1, json=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_get(args)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, ["http://1.2.3.4:8080"])

    def test_get_empty(self):
        self.container.get_proxies.execute.return_value = []
        args = _Args(n=1, json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_get(args)
        self.assertEqual(buf.getvalue(), "")

    def test_get_saves_cache(self):
        self.container.get_proxies.execute.return_value = ["http://1.2.3.4:8080"]
        args = _Args(n=1, json=False)
        cli.cmd_get(args)
        self.container.save_full.assert_called_once()


class TestCmdStatus(ContainerTestCase):
    def test_status_empty(self):
        args = _Args()
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("default-http", output)
        self.assertIn("trusted", output)
        self.assertIn("blacklist", output)
        self.assertIn("freeproxy-world", output)
        self.assertIn("可用代理总数: 0", output)
        self.assertIn("无暂停的代理源", output)

    def test_status_with_proxies(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        self.container.pool.add_to_group("trusted", "http://9.9.9.9:80")
        args = _Args()
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("可用代理总数: 2", output)

    def test_status_with_paused_sources(self):
        self.container.pool.pause_source("src.interfaces.sources.ip89")
        args = _Args()
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("暂停的代理源", output)
        self.assertIn("src.interfaces.sources.ip89", output)

    def test_status_shows_ttl(self):
        args = _Args()
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("永不过期", output)
        self.assertIn(f"{cli.config.PROXY_GROUPS['default-http']}s", output)


class TestCmdBlacklist(ContainerTestCase):
    def test_blacklist_single(self):
        self.container.pool.add_to_group("default-http", "http://1.2.3.4:8080")
        args = _Args(proxies=["http://1.2.3.4:8080"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_blacklist(args)
        self.assertTrue(self.container.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertIn("已加入黑名单: http://1.2.3.4:8080", buf.getvalue())

    def test_blacklist_multiple(self):
        args = _Args(proxies=["http://1.2.3.4:8080", "http://5.6.7.8:3128"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_blacklist(args)
        self.assertTrue(self.container.pool.is_blacklisted("http://1.2.3.4:8080"))
        self.assertTrue(self.container.pool.is_blacklisted("http://5.6.7.8:3128"))
        output = buf.getvalue()
        self.assertIn("http://1.2.3.4:8080", output)
        self.assertIn("http://5.6.7.8:3128", output)

    def test_blacklist_saves(self):
        args = _Args(proxies=["http://1.2.3.4:8080"])
        cli.cmd_blacklist(args)
        pool2 = make_pool(self.db_path)
        self.assertTrue(pool2.is_blacklisted("http://1.2.3.4:8080"))


class TestCmdTrust(ContainerTestCase):
    def test_trust_single(self):
        args = _Args(proxies=["http://1.2.3.4:8080"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_trust(args)
        self.assertIn("http://1.2.3.4:8080", self.container.pool.proxy_groups["trusted"]["proxies"])
        self.assertIn("已加入信任池", buf.getvalue())

    def test_trust_multiple(self):
        args = _Args(proxies=["http://1.2.3.4:8080", "http://5.6.7.8:3128"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_trust(args)
        self.assertIn("http://1.2.3.4:8080", self.container.pool.proxy_groups["trusted"]["proxies"])
        self.assertIn("http://5.6.7.8:3128", self.container.pool.proxy_groups["trusted"]["proxies"])

    def test_trust_saves(self):
        args = _Args(proxies=["http://1.2.3.4:8080"])
        cli.cmd_trust(args)
        pool2 = make_pool(self.db_path)
        self.assertIn("http://1.2.3.4:8080", pool2.proxy_groups["trusted"]["proxies"])


class TestCmdRequest(ContainerTestCase):
    def test_request_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "hello world"
        self.container.request_with_retry.execute.return_value = ("http://1.2.3.4:8080", mock_resp)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("使用代理: http://1.2.3.4:8080", output)
        self.assertIn("状态码: 200", output)
        self.assertIn("hello world", output)

    def test_request_failure(self):
        self.container.request_with_retry.execute.side_effect = Exception("timeout")
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            with self.assertRaises(SystemExit):
                cli.cmd_request(args)

    def test_request_no_available_proxy(self):
        self.container.request_with_retry.execute.side_effect = RuntimeError("无可用代理")
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_request(args)
            self.assertEqual(ctx.exception.code, 1)

    def test_request_max_length_truncation(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "A" * 100
        self.container.request_with_retry.execute.return_value = ("http://1.2.3.4:8080", mock_resp)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=10, headers=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("截断", output)
        self.assertIn("100 字节", output)

    def test_request_headers_only(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.text = "should not appear"
        self.container.request_with_retry.execute.return_value = ("http://1.2.3.4:8080", mock_resp)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("Content-Type", output)
        self.assertNotIn("should not appear", output)

    def test_request_saves_cache_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "ok"
        self.container.request_with_retry.execute.return_value = ("http://1.2.3.4:8080", mock_resp)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        cli.cmd_request(args)
        self.container.save_full.assert_called_once()

    def test_request_saves_cache_on_failure(self):
        self.container.request_with_retry.execute.side_effect = Exception("fail")
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with self.assertRaises(SystemExit):
            cli.cmd_request(args)
        self.container.save_full.assert_called_once()

    def test_request_passes_retries(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "ok"
        self.container.request_with_retry.execute.return_value = ("http://1.2.3.4:8080", mock_resp)
        args = _Args(url="http://example.com", timeout=10, retries=3, max_length=None, headers=False)
        cli.cmd_request(args)
        _, kwargs = self.container.request_with_retry.execute.call_args
        self.assertEqual(kwargs["max_retries"], 3)


class TestCmdTest(ContainerTestCase):
    def test_test_single_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        args = _Args(proxy="http://1.2.3.4:8080", url="http://example.com", timeout=10, concurrent=0)
        with patch.object(cli.http_client, "get", return_value=mock_resp) as mock_get:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_test(args)
        output = buf.getvalue()
        self.assertIn("成功: http://1.2.3.4:8080", output)
        self.assertIn("状态码: 200", output)
        self.assertIn("耗时:", output)

    def test_test_single_failure(self):
        args = _Args(proxy="http://1.2.3.4:8080", url="http://example.com", timeout=10, concurrent=0)
        with patch.object(cli.http_client, "get", side_effect=Exception("refused")) as mock_get:
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.cmd_test(args)
                self.assertEqual(ctx.exception.code, 1)
        output = buf.getvalue()
        self.assertIn("失败: http://1.2.3.4:8080", output)
        self.assertIn("refused", output)
        self.assertIn("耗时:", output)

    def test_test_timeout_param(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        args = _Args(proxy="http://1.2.3.4:8080", url="http://example.com", timeout=5, concurrent=0)
        with patch.object(cli.http_client, "get", return_value=mock_resp) as mock_get:
            cli.cmd_test(args)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], 5)

    def test_test_concurrent_mode(self):
        self.container.pool.get_available_proxies = MagicMock(return_value={
            "http://1.1.1.1:8080": {"group": "default-http", "consecutive_fails": 0},
            "http://2.2.2.2:3128": {"group": "default-http", "consecutive_fails": 0},
        })
        mock_results = {
            "http://1.1.1.1:8080": {"success": True, "status_code": 200, "elapsed": 0.5, "error": None},
            "http://2.2.2.2:3128": {"success": False, "status_code": None, "elapsed": 10.0, "error": "Timeout"},
        }
        self.container.check_proxies.execute = AsyncMock(return_value=mock_results)
        args = _Args(proxy=None, url="http://example.com", timeout=10, concurrent=2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_test(args)
        output = buf.getvalue()
        self.assertIn("http://1.1.1.1:8080", output)
        self.assertIn("http://2.2.2.2:3128", output)
        self.assertIn("成功", output)
        self.assertIn("失败", output)

    def test_test_concurrent_no_proxies(self):
        self.container.pool.get_available_proxies = MagicMock(return_value={})
        args = _Args(proxy=None, url="http://example.com", timeout=10, concurrent=5)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            with self.assertRaises(SystemExit):
                cli.cmd_test(args)


class TestMainArgParsing(unittest.TestCase):
    def test_no_command_exits(self):
        with self.assertRaises(SystemExit):
            cli.main()

    def test_fetch_command(self):
        with patch.object(cli, "cmd_fetch") as mock_cmd:
            with patch("sys.argv", ["cli.py", "fetch"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_list_command(self):
        with patch.object(cli, "cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "list"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_ls_alias(self):
        with patch.object(cli, "cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "ls"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_get_command_with_n(self):
        with patch.object(cli, "cmd_get") as mock_cmd:
            with patch("sys.argv", ["cli.py", "get", "-n", "3"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.n, 3)

    def test_status_command(self):
        with patch.object(cli, "cmd_status") as mock_cmd:
            with patch("sys.argv", ["cli.py", "status"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_blacklist_command(self):
        with patch.object(cli, "cmd_blacklist") as mock_cmd:
            with patch("sys.argv", ["cli.py", "blacklist", "http://1.2.3.4:8080"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxies, ["http://1.2.3.4:8080"])

    def test_trust_command(self):
        with patch.object(cli, "cmd_trust") as mock_cmd:
            with patch("sys.argv", ["cli.py", "trust", "http://5.6.7.8:3128"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxies, ["http://5.6.7.8:3128"])

    def test_request_command(self):
        with patch.object(cli, "cmd_request") as mock_cmd:
            with patch("sys.argv", ["cli.py", "request", "http://example.com"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.url, "http://example.com")

    def test_request_with_options(self):
        with patch.object(cli, "cmd_request") as mock_cmd:
            with patch("sys.argv", ["cli.py", "request", "http://example.com", "--timeout", "5", "--max-length", "1000", "--headers"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.timeout, 5)
        self.assertEqual(args.max_length, 1000)
        self.assertTrue(args.headers)

    def test_test_command(self):
        with patch.object(cli, "cmd_test") as mock_cmd:
            with patch("sys.argv", ["cli.py", "test", "http://1.2.3.4:8080", "http://example.com"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxy, "http://1.2.3.4:8080")
        self.assertEqual(args.url, "http://example.com")

    def test_debug_flag(self):
        # --debug 时文件日志也降为 DEBUG（控制台本就默认 DEBUG）
        with patch.object(cli, "cmd_fetch"), \
             patch.object(cli, "setup_logging") as mock_setup:
            with patch("sys.argv", ["cli.py", "--debug", "fetch"]):
                cli.main()
        mock_setup.assert_called_once_with(file_level=logging.DEBUG)

    def test_default_logging_file_level(self):
        # 默认：控制台 DEBUG + 文件 INFO（setup_logging 缺省 file_level）
        with patch.object(cli, "cmd_fetch"), \
             patch.object(cli, "setup_logging") as mock_setup:
            with patch("sys.argv", ["cli.py", "fetch"]):
                cli.main()
        mock_setup.assert_called_once_with()

    def test_list_group_option(self):
        with patch.object(cli, "cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "list", "--group", "trusted"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.group, "trusted")

    def test_list_json_option(self):
        with patch.object(cli, "cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "ls", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)

    def test_serve_command_defaults(self):
        with patch.object(cli, "cmd_serve") as mock_cmd:
            with patch("sys.argv", ["cli.py", "serve"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.host, cli.config.SERVER_HOST)
        self.assertEqual(args.port, cli.config.SERVER_PORT)
        self.assertEqual(args.interval, cli.config.HEARTBEAT_INTERVAL)
        self.assertEqual(args.queue_size, cli.config.STANDBY_QUEUE_CAPACITY)
        self.assertIsNone(args.type)

    def test_serve_command_options(self):
        with patch.object(cli, "cmd_serve") as mock_cmd:
            with patch("sys.argv", ["cli.py", "serve", "--host", "0.0.0.0", "--port", "9000", "--interval", "30", "--type", "http", "--type", "socks5"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.interval, 30)
        self.assertEqual(args.type, ["http", "socks5"])

    def test_fetch_json_option(self):
        with patch.object(cli, "cmd_fetch") as mock_cmd:
            with patch("sys.argv", ["cli.py", "fetch", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)

    def test_get_json_option(self):
        with patch.object(cli, "cmd_get") as mock_cmd:
            with patch("sys.argv", ["cli.py", "get", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
