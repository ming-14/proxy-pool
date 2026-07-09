import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from cache import CacheManager
from config import MAX_FAILS, TRUSTED_MAX_FAILS, PROXY_GROUPS
from manager import ProxyManager
import cli


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestPrintProxies(unittest.TestCase):
    def test_plain_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies(["1.2.3.4:8080", "5.6.7.8:3128"])
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(lines, ["1.2.3.4:8080", "5.6.7.8:3128"])

    def test_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print_proxies(["1.2.3.4:8080", "5.6.7.8:3128"], as_json=True)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, ["1.2.3.4:8080", "5.6.7.8:3128"])

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


class TestCmdFetch(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")
        self._patcher_cm = patch("cli.CacheManager", lambda cache_file=None: CacheManager(cache_file=self.cache_file))
        self._patcher_pm = patch("cli.ProxyManager", lambda: ProxyManager(cache_file=self.cache_file))
        self._patcher_cm.start()
        self._patcher_pm.start()

    def tearDown(self):
        self._patcher_cm.stop()
        self._patcher_pm.stop()
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_fetch_plain(self):
        args = _Args(json=False)
        mock_pm = MagicMock()
        mock_pm._cache.get_available_proxies.return_value = {"1.2.3.4:8080": {"group": "default", "fails": 0}}
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_fetch(args)
        self.assertIn("1.2.3.4:8080", buf.getvalue())
        mock_pm._fetch_proxies.assert_called_once()
        mock_pm.save_cache.assert_called_once()

    def test_fetch_json(self):
        args = _Args(json=True)
        mock_pm = MagicMock()
        mock_pm._cache.get_available_proxies.return_value = {"1.2.3.4:8080": {"group": "default", "fails": 0}}
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_fetch(args)
        data = json.loads(buf.getvalue())
        self.assertIn("1.2.3.4:8080", data)

    def test_fetch_empty(self):
        args = _Args(json=False)
        mock_pm = MagicMock()
        mock_pm._cache.get_available_proxies.return_value = {}
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_fetch(args)
        self.assertEqual(buf.getvalue().strip(), "")

    def test_fetch_saves_cache_on_exception(self):
        args = _Args(json=False)
        mock_pm = MagicMock()
        mock_pm._fetch_proxies.side_effect = RuntimeError("boom")
        with patch("cli.ProxyManager", return_value=mock_pm):
            with self.assertRaises(RuntimeError):
                cli.cmd_fetch(args)
        mock_pm.save_cache.assert_called_once()


class TestCmdList(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_list_all(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("trusted", "9.9.9.9:80")
        args = _Args(group=None, json=False)
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_list(args)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("1.2.3.4:8080", lines)
        self.assertIn("9.9.9.9:80", lines)

    def test_list_by_group(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("trusted", "9.9.9.9:80")
        args = _Args(group="trusted", json=False)
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_list(args)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(lines, ["9.9.9.9:80"])

    def test_list_json(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        args = _Args(group=None, json=True)
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_list(args)
        data = json.loads(buf.getvalue())
        self.assertIn("1.2.3.4:8080", data)

    def test_list_empty(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(group=None, json=False)
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_list(args)
        self.assertEqual(buf.getvalue(), "")

    def test_ls_alias(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        args = _Args(group=None, json=False)
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_list(args)
        self.assertIn("1.2.3.4:8080", buf.getvalue())


class TestCmdGet(unittest.TestCase):
    def test_get_one(self):
        mock_pm = MagicMock()
        mock_pm.get_proxies.return_value = ["1.2.3.4:8080"]
        args = _Args(n=1, json=False)
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_get(args)
        self.assertEqual(buf.getvalue().strip(), "1.2.3.4:8080")
        mock_pm.get_proxies.assert_called_once_with(1)

    def test_get_multiple(self):
        mock_pm = MagicMock()
        mock_pm.get_proxies.return_value = ["1.2.3.4:8080", "5.6.7.8:3128"]
        args = _Args(n=2, json=False)
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_get(args)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        mock_pm.get_proxies.assert_called_once_with(2)

    def test_get_json(self):
        mock_pm = MagicMock()
        mock_pm.get_proxies.return_value = ["1.2.3.4:8080"]
        args = _Args(n=1, json=True)
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_get(args)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, ["1.2.3.4:8080"])

    def test_get_empty(self):
        mock_pm = MagicMock()
        mock_pm.get_proxies.return_value = []
        args = _Args(n=1, json=False)
        with patch("cli.ProxyManager", return_value=mock_pm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_get(args)
        self.assertEqual(buf.getvalue(), "")

    def test_get_saves_cache(self):
        mock_pm = MagicMock()
        mock_pm.get_proxies.return_value = ["1.2.3.4:8080"]
        args = _Args(n=1, json=False)
        with patch("cli.ProxyManager", return_value=mock_pm):
            cli.cmd_get(args)
        mock_pm.save_cache.assert_called_once()


class TestCmdStatus(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_status_empty(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args()
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("default", output)
        self.assertIn("trusted", output)
        self.assertIn("blacklist", output)
        self.assertIn("freeproxy-world", output)
        self.assertIn("可用代理总数: 0", output)
        self.assertIn("无暂停的代理源", output)

    def test_status_with_proxies(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        cm.add_to_group("trusted", "9.9.9.9:80")
        args = _Args()
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("可用代理总数: 2", output)

    def test_status_with_paused_sources(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.pause_source("proxy_pool.89ip-cn")
        args = _Args()
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("暂停的代理源", output)
        self.assertIn("proxy_pool.89ip-cn", output)

    def test_status_shows_ttl(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args()
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
        output = buf.getvalue()
        self.assertIn("永不过期", output)
        self.assertIn("240s", output)


class TestCmdBlacklist(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_blacklist_single(self):
        cm = CacheManager(cache_file=self.cache_file)
        cm.add_to_group("default", "1.2.3.4:8080")
        args = _Args(proxies=["1.2.3.4:8080"])
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_blacklist(args)
        self.assertTrue(cm.is_blacklisted("1.2.3.4:8080"))
        self.assertIn("已加入黑名单: 1.2.3.4:8080", buf.getvalue())

    def test_blacklist_multiple(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(proxies=["1.2.3.4:8080", "5.6.7.8:3128"])
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_blacklist(args)
        self.assertTrue(cm.is_blacklisted("1.2.3.4:8080"))
        self.assertTrue(cm.is_blacklisted("5.6.7.8:3128"))
        output = buf.getvalue()
        self.assertIn("1.2.3.4:8080", output)
        self.assertIn("5.6.7.8:3128", output)

    def test_blacklist_saves(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(proxies=["1.2.3.4:8080"])
        with patch("cli.CacheManager", return_value=cm):
            cli.cmd_blacklist(args)
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertTrue(cm2.is_blacklisted("1.2.3.4:8080"))


class TestCmdTrust(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, ".cache")

    def tearDown(self):
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        os.rmdir(self.tmp_dir)

    def test_trust_single(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(proxies=["1.2.3.4:8080"])
        with patch("cli.CacheManager", return_value=cm):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_trust(args)
        self.assertIn("1.2.3.4:8080", cm.proxy_groups["trusted"]["proxies"])
        self.assertIn("已加入信任池", buf.getvalue())

    def test_trust_multiple(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(proxies=["1.2.3.4:8080", "5.6.7.8:3128"])
        with patch("cli.CacheManager", return_value=cm):
            cli.cmd_trust(args)
        self.assertIn("1.2.3.4:8080", cm.proxy_groups["trusted"]["proxies"])
        self.assertIn("5.6.7.8:3128", cm.proxy_groups["trusted"]["proxies"])

    def test_trust_saves(self):
        cm = CacheManager(cache_file=self.cache_file)
        args = _Args(proxies=["1.2.3.4:8080"])
        with patch("cli.CacheManager", return_value=cm):
            cli.cmd_trust(args)
        cm2 = CacheManager(cache_file=self.cache_file)
        self.assertIn("1.2.3.4:8080", cm2.proxy_groups["trusted"]["proxies"])


class TestCmdRequest(unittest.TestCase):
    def test_request_success(self):
        pm = MagicMock(spec=ProxyManager)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "hello world"
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", return_value=("1.2.3.4:8080", mock_resp)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("使用代理: 1.2.3.4:8080", output)
        self.assertIn("状态码: 200", output)
        self.assertIn("hello world", output)

    def test_request_failure(self):
        pm = MagicMock(spec=ProxyManager)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", side_effect=Exception("timeout")):
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                with self.assertRaises(SystemExit):
                    cli.cmd_request(args)

    def test_request_no_available_proxy(self):
        pm = MagicMock(spec=ProxyManager)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", side_effect=RuntimeError("无可用代理")):
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                with self.assertRaises(SystemExit) as ctx:
                    cli.cmd_request(args)
                self.assertEqual(ctx.exception.code, 1)

    def test_request_max_length_truncation(self):
        pm = MagicMock(spec=ProxyManager)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "A" * 100
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=10, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", return_value=("1.2.3.4:8080", mock_resp)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("截断", output)
        self.assertIn("100 字节", output)

    def test_request_headers_only(self):
        pm = MagicMock(spec=ProxyManager)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.text = "should not appear"
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=True)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", return_value=("1.2.3.4:8080", mock_resp)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_request(args)
        output = buf.getvalue()
        self.assertIn("Content-Type", output)
        self.assertNotIn("should not appear", output)

    def test_request_saves_cache_on_success(self):
        pm = MagicMock(spec=ProxyManager)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "ok"
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", return_value=("1.2.3.4:8080", mock_resp)):
            cli.cmd_request(args)
        pm.save_cache.assert_called_once()

    def test_request_saves_cache_on_failure(self):
        pm = MagicMock(spec=ProxyManager)
        args = _Args(url="http://example.com", timeout=10, retries=5, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", side_effect=Exception("fail")):
            with self.assertRaises(SystemExit):
                cli.cmd_request(args)
        pm.save_cache.assert_called_once()

    def test_request_passes_retries(self):
        pm = MagicMock(spec=ProxyManager)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.text = "ok"
        args = _Args(url="http://example.com", timeout=10, retries=3, max_length=None, headers=False)
        with patch("cli.ProxyManager", return_value=pm), \
             patch("cli.request_with_retry", return_value=("1.2.3.4:8080", mock_resp)) as mock_rwr:
            cli.cmd_request(args)
        mock_rwr.assert_called_once()
        _, kwargs = mock_rwr.call_args
        self.assertEqual(kwargs["max_retries"], 3)


class TestCmdTest(unittest.TestCase):
    def test_test_single_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        args = _Args(proxy="1.2.3.4:8080", url="http://example.com", timeout=10, concurrent=0)
        with patch("cli.requests.get", return_value=mock_resp):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_test(args)
        output = buf.getvalue()
        self.assertIn("成功: 1.2.3.4:8080", output)
        self.assertIn("状态码: 200", output)
        self.assertIn("耗时:", output)

    def test_test_single_failure(self):
        args = _Args(proxy="1.2.3.4:8080", url="http://example.com", timeout=10, concurrent=0)
        with patch("cli.requests.get", side_effect=Exception("refused")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.cmd_test(args)
                self.assertEqual(ctx.exception.code, 1)
        output = buf.getvalue()
        self.assertIn("失败: 1.2.3.4:8080", output)
        self.assertIn("refused", output)
        self.assertIn("耗时:", output)

    def test_test_timeout_param(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        args = _Args(proxy="1.2.3.4:8080", url="http://example.com", timeout=5, concurrent=0)
        with patch("cli.requests.get", return_value=mock_resp) as mock_get:
            cli.cmd_test(args)
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], 5)

    def test_test_concurrent_mode(self):
        cm = MagicMock()
        cm.get_available_proxies.return_value = {
            "1.1.1.1:8080": {"group": "default", "fails": 0},
            "2.2.2.2:3128": {"group": "default", "fails": 0},
        }
        mock_results = {
            "1.1.1.1:8080": {"success": True, "status_code": 200, "elapsed": 0.5, "error": None},
            "2.2.2.2:3128": {"success": False, "status_code": None, "elapsed": 10.0, "error": "Timeout"},
        }
        args = _Args(proxy=None, url="http://example.com", timeout=10, concurrent=2)
        with patch("cli.CacheManager", return_value=cm), \
             patch("cli.check_proxies_concurrent", return_value=mock_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_test(args)
        output = buf.getvalue()
        self.assertIn("1.1.1.1:8080", output)
        self.assertIn("2.2.2.2:3128", output)
        self.assertIn("成功", output)
        self.assertIn("失败", output)

    def test_test_concurrent_no_proxies(self):
        cm = MagicMock()
        cm.get_available_proxies.return_value = {}
        args = _Args(proxy=None, url="http://example.com", timeout=10, concurrent=5)
        with patch("cli.CacheManager", return_value=cm):
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                with self.assertRaises(SystemExit):
                    cli.cmd_test(args)


class TestMainArgParsing(unittest.TestCase):
    def test_no_command_exits(self):
        with self.assertRaises(SystemExit):
            cli.main()

    def test_fetch_command(self):
        with patch("cli.cmd_fetch") as mock_cmd:
            with patch("sys.argv", ["cli.py", "fetch"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_list_command(self):
        with patch("cli.cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "list"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_ls_alias(self):
        with patch("cli.cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "ls"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_get_command_with_n(self):
        with patch("cli.cmd_get") as mock_cmd:
            with patch("sys.argv", ["cli.py", "get", "-n", "3"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.n, 3)

    def test_status_command(self):
        with patch("cli.cmd_status") as mock_cmd:
            with patch("sys.argv", ["cli.py", "status"]):
                cli.main()
        mock_cmd.assert_called_once()

    def test_blacklist_command(self):
        with patch("cli.cmd_blacklist") as mock_cmd:
            with patch("sys.argv", ["cli.py", "blacklist", "1.2.3.4:8080"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxies, ["1.2.3.4:8080"])

    def test_trust_command(self):
        with patch("cli.cmd_trust") as mock_cmd:
            with patch("sys.argv", ["cli.py", "trust", "5.6.7.8:3128"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxies, ["5.6.7.8:3128"])

    def test_request_command(self):
        with patch("cli.cmd_request") as mock_cmd:
            with patch("sys.argv", ["cli.py", "request", "http://example.com"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.url, "http://example.com")

    def test_request_with_options(self):
        with patch("cli.cmd_request") as mock_cmd:
            with patch("sys.argv", ["cli.py", "request", "http://example.com", "--timeout", "5", "--max-length", "1000", "--headers"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.timeout, 5)
        self.assertEqual(args.max_length, 1000)
        self.assertTrue(args.headers)

    def test_test_command(self):
        with patch("cli.cmd_test") as mock_cmd:
            with patch("sys.argv", ["cli.py", "test", "1.2.3.4:8080", "http://example.com"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.proxy, "1.2.3.4:8080")
        self.assertEqual(args.url, "http://example.com")

    def test_debug_flag(self):
        with patch("cli.cmd_fetch"), \
             patch("cli.logging.getLogger") as mock_logger:
            with patch("sys.argv", ["cli.py", "--debug", "fetch"]):
                cli.main()
        mock_logger.assert_called()

    def test_list_group_option(self):
        with patch("cli.cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "list", "--group", "trusted"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertEqual(args.group, "trusted")

    def test_list_json_option(self):
        with patch("cli.cmd_list") as mock_cmd:
            with patch("sys.argv", ["cli.py", "ls", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)

    def test_fetch_json_option(self):
        with patch("cli.cmd_fetch") as mock_cmd:
            with patch("sys.argv", ["cli.py", "fetch", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)

    def test_get_json_option(self):
        with patch("cli.cmd_get") as mock_cmd:
            with patch("sys.argv", ["cli.py", "get", "--json"]):
                cli.main()
        args = mock_cmd.call_args[0][0]
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
