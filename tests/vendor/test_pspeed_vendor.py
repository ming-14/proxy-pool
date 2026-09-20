"""vendor/pspeed 本地维护回归测试

覆盖本项目已确认的测速缺陷，逐一内化修复并回归：
- `--multi N` 语义名不副实：首轮成功即 break，multi>1 与 multi=1 等价（README
  承诺的是"每节点重复 N 次取峰值"）
- 未知节点名静默跳过 → probe_one 仍判 ok=True，产出"通过但没速度"的假结果
- hard_limit 不是硬时限：写死 timeout=(5, 12)，面板设 10s 实测最坏 ~17s
- 下载速率含 TTFB 而上传只算传输阶段，两个维度不可比（慢代理上出现数十倍假差距）
- tcp_ping 硬编码 AF_INET：IPv6 代理一律判为不通而遭误淘汰

全部离线：subprocess 与 requests 均被替换，时钟可控。
"""

from __future__ import annotations

import asyncio
import io
import itertools
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PSPEED_SRC = os.path.join(_PROJECT_ROOT, "vendor", "pspeed", "src")
if _PSPEED_SRC not in sys.path:
    sys.path.insert(0, _PSPEED_SRC)

from pspeed.cli import main as cli_main  # noqa: E402
from pspeed.nodes import (  # noqa: E402
    DOWNLOAD_NODES, UPLOAD_NODES, DownloadNode, get_download_nodes, get_upload_nodes,
)
from pspeed.probe import (  # noqa: E402
    _curl_download, _download_once, _requests_download, _requests_upload,
    _transfer_mbps, download, probe_one, tcp_ping, upload,
)
from pspeed.proxy import parse_one  # noqa: E402

_PROXY = parse_one("http://1.2.3.4:8080")
_DL_NODE = next(n for n in DOWNLOAD_NODES if n.name == "cloudflare")
_UL_NODE = next(n for n in UPLOAD_NODES if n.name == "cloudflare")


def _sample(node: str, mbps: float, ok: bool = True) -> dict:
    return {"ok": ok, "bytes": 1000, "mbps": mbps, "ms": 10.0, "node": node, "err": None}


class _FakeResponse:
    def __init__(self, chunks):
        self.status_code = 200
        self._chunks = chunks

    def iter_content(self, size):
        yield from self._chunks

    def close(self):
        pass


class _FakeSession:
    """记录 sess.get 实参的假 Session；chunks 为可迭代的分块序列"""

    proxies = {"http": "http://1.2.3.4:8080"}

    def __init__(self, chunks=()):
        self._chunks = chunks
        self.get_kwargs: dict | None = None
        self.post_kwargs: dict | None = None

    def get(self, url, **kwargs):
        self.get_kwargs = kwargs
        return _FakeResponse(self._chunks)

    def post(self, url, **kwargs):
        self.post_kwargs = kwargs
        return _FakeResponse(())


class TestMultiSemantics(unittest.TestCase):
    """multi 必须真的重复 N 轮并取峰值（原先首轮成功即 break，等价于 multi=1）"""

    def test_download_repeats_every_round_and_keeps_peak(self):
        speeds = [1.0, 3.0, 2.0]

        def fake_once(sess, node, want, hard):
            return _sample(node.name, speeds.pop(0))

        with mock.patch("pspeed.probe._download_once", side_effect=fake_once) as once:
            res = download(_PROXY, [_DL_NODE], 1000, multi=3, hard_limit=1.0)

        self.assertEqual(once.call_count, 3)
        self.assertEqual(len(res["samples"]), 3)
        self.assertEqual(res["best_mbps"], 3.0)
        self.assertEqual(res["avg_mbps"], 2.0)

    def test_upload_repeats_every_round_and_keeps_peak(self):
        speeds = [2.0, 5.0]

        def fake_once(sess, node, payload, hard):
            return _sample(node.name, speeds.pop(0))

        with mock.patch("pspeed.probe._upload_once", side_effect=fake_once) as once:
            res = upload(_PROXY, [_UL_NODE], 1000, multi=2, hard_limit=1.0)

        self.assertEqual(once.call_count, 2)
        self.assertEqual(res["best_mbps"], 5.0)

    def test_multi_one_runs_single_round(self):
        with mock.patch("pspeed.probe._download_once", return_value=_sample("cloudflare", 1.5)) as once:
            download(_PROXY, [_DL_NODE], 1000, multi=1, hard_limit=1.0)
        self.assertEqual(once.call_count, 1)


class TestNodeSelection(unittest.TestCase):
    """未知节点名必须报错，不能静默变成空节点列表"""

    def test_unknown_download_node_raises(self):
        with self.assertRaises(ValueError) as ctx:
            get_download_nodes(["cloudflare", "typo-node"])
        self.assertIn("typo-node", str(ctx.exception))

    def test_unknown_upload_node_raises(self):
        with self.assertRaises(ValueError) as ctx:
            get_upload_nodes(["typo-node"])
        self.assertIn("typo-node", str(ctx.exception))

    def test_all_and_empty_still_select_every_node(self):
        self.assertEqual(len(get_download_nodes(["all"])), len(DOWNLOAD_NODES))
        self.assertEqual(len(get_download_nodes(None)), len(DOWNLOAD_NODES))
        self.assertEqual(len(get_upload_nodes(["all"])), len(UPLOAD_NODES))

    def test_probe_one_rejects_dimension_without_nodes(self):
        with self.assertRaises(ValueError):
            probe_one(_PROXY, ["download"], dl_nodes=[])
        with self.assertRaises(ValueError):
            probe_one(_PROXY, ["upload"], ul_nodes=[])


class TestTransferWindow(unittest.TestCase):
    """两个方向共用同一套速率口径：传输窗口优先，窗口过短退回全程耗时"""

    def test_window_excludes_connect_and_ttfb(self):
        # 1.25MB / 10s 传输窗口 = 1.0 Mbps（含 TTFB 的 12s 全程口径会算成 0.833）
        self.assertEqual(_transfer_mbps(1_250_000, 10.0, 12.0), 1.0)

    def test_short_window_falls_back_to_elapsed(self):
        self.assertEqual(_transfer_mbps(1_250_000, 0.1, 2.5), 4.0)

    def test_degenerate_inputs(self):
        self.assertEqual(_transfer_mbps(0, 1.0, 1.0), 0.0)
        self.assertEqual(_transfer_mbps(1000, 0.0, 0.0), 0.0)


class TestCurlDownload(unittest.TestCase):
    def _run(self, stdout: str, returncode: int = 0, hard_limit: float = 10.0):
        done = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
        with mock.patch("pspeed.probe.subprocess.run", return_value=done) as run:
            res = _curl_download("/usr/bin/curl", "http://1.2.3.4:8080", _DL_NODE,
                                 1_250_000, hard_limit=hard_limit)
        return res, run.call_args.args[0]

    def test_rate_comes_from_transfer_window_and_limit_is_enforced(self):
        res, cmd = self._run("200 1250000 2.0 12.0")
        self.assertIn("--max-time", cmd)
        self.assertEqual(cmd[cmd.index("--max-time") + 1], "10")
        self.assertEqual(res["ok"], True)
        self.assertEqual(res["mbps"], 1.0)   # 1250000B×8 / 10s 传输窗口
        self.assertEqual(res["bytes"], 1_250_000)
        self.assertEqual(res["ms"], 12000.0)

    def test_truncated_transfer_still_measures(self):
        """被 --max-time 截断时 curl 仍输出统计：部分传输也能算出速率"""
        res, _ = self._run("200 500000 1.5 10.0", returncode=28)
        self.assertEqual(res["ok"], True)
        self.assertEqual(res["mbps"], 0.471)  # 4000000 bits / 8.5s 窗口

    def test_no_response_reports_curl_exit_code(self):
        res, _ = self._run("0 0 0.0 10.0", returncode=28)
        self.assertEqual(res["ok"], False)
        self.assertIn("curl exit 28", res["err"])

    def test_http_error_code_is_reported(self):
        res, _ = self._run("403 0 0.1 0.2")
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["err"], "HTTP 403")

    def test_malformed_output_is_reported(self):
        res, _ = self._run("garbage")
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["err"], "curl 无输出")


class TestRequestsFallback(unittest.TestCase):
    """curl 不可用时回退 requests：口径一致 + 时限受 hard_limit 约束"""

    def test_timeouts_clamped_to_hard_limit(self):
        sess = _FakeSession()
        _requests_download(sess, _DL_NODE, 1024, hard_limit=2.0)
        self.assertEqual(sess.get_kwargs["timeout"], (2.0, 2.0))

    def test_timeouts_keep_caps_when_limit_is_large(self):
        sess = _FakeSession()
        _requests_download(sess, _DL_NODE, 1024, hard_limit=30.0)
        self.assertEqual(sess.get_kwargs["timeout"], (5.0, 12.0))

    def test_upload_timeouts_clamped_to_hard_limit(self):
        sess = _FakeSession()
        _requests_upload(sess, _UL_NODE, b"x" * 1024, hard_limit=2.0)
        self.assertEqual(sess.post_kwargs["timeout"], (2.0, 2.0))

    def test_deadline_stops_streaming(self):
        """时钟每调用一次前进 1s：hard_limit=3 时收满 3 个 chunk 即截止"""
        clock = itertools.count(start=0.0, step=1.0)
        seen = 0

        def chunks():
            nonlocal seen
            while True:
                seen += 1
                yield b"x" * 100

        sess = _FakeSession(chunks())
        with mock.patch("pspeed.probe._now", side_effect=lambda: next(clock)):
            res = _requests_download(sess, _DL_NODE, 10 ** 9, hard_limit=3.0)

        self.assertEqual(seen, 3)          # t0=0, deadline=3：第 3 个 chunk 后截止
        self.assertEqual(res["bytes"], 300)

    def test_falls_back_only_when_curl_missing(self):
        sess = _FakeSession([b"x" * 2048])
        with mock.patch("pspeed.probe.shutil.which", return_value=None), \
                mock.patch("pspeed.probe._requests_download", return_value=_sample("cloudflare", 1.0)) as req:
            _download_once(sess, _DL_NODE, 2048, hard_limit=1.0)
        self.assertEqual(req.call_count, 1)

    def test_prefers_curl_when_available(self):
        sess = _FakeSession()
        with mock.patch("pspeed.probe.shutil.which", return_value="/usr/bin/curl"), \
                mock.patch("pspeed.probe._curl_download", return_value=_sample("cloudflare", 1.0)) as cur:
            _download_once(sess, _DL_NODE, 2048, hard_limit=1.0)
        self.assertEqual(cur.call_count, 1)


async def _slow_trickle(reader, writer):
    """假代理：无论请求内容，持续每 0.2s 发 4KB（永不结束，逼出硬时限）"""
    try:
        await reader.read(4096)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n")
        await writer.drain()
        while True:
            writer.write(b"x" * 4096)
            await writer.drain()
            await asyncio.sleep(0.2)
    except Exception:
        pass


class TestCurlAgainstLocalProxy(unittest.TestCase):
    """curl 下载路径的真实链路验证：真起 curl 子进程、经本地 mock 代理转发

    py 层 mock 盖不到的部分：--max-time/-x/-w 参数 curl 是否接受、-w 输出能否解析、
    请求是否真的过代理。全程本机，不依赖外网。
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("curl"):
            raise unittest.SkipTest("环境无 curl（requests 回退路径另有用例覆盖）")
        from tests.e2e.mock_upstream import MOCK_BODY, start_mock_upstream

        cls.body_len = len(MOCK_BODY)
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()
        fut = asyncio.run_coroutine_threadsafe(start_mock_upstream(), cls.loop)
        cls.server, cls.port = fut.result(timeout=10)

    @classmethod
    def tearDownClass(cls):
        async def _shutdown() -> None:
            cls.server.close()
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for t in pending:                      # 服完在读的连接处理任务（如永不结束的 trickle）
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(_shutdown(), cls.loop).result(timeout=5)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=5)
        cls.loop.close()

    def test_download_through_local_proxy_reports_body_within_limit(self):
        proxy = parse_one(f"http://127.0.0.1:{self.port}")
        node = DownloadNode("local", "本机", "http://node.local/10mb.test")

        res = download(proxy, [node], self.body_len, multi=1, hard_limit=10.0)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["bytes"], self.body_len)  # 完整拿到 mock 响应体
        self.assertGreater(res["best_mbps"], 0)
        self.assertLessEqual(res["ms"], 10_000)       # 总耗时不超 hard_limit

    def test_hard_limit_bounds_wall_clock_on_endless_transfer(self):
        """硬时限的真实测量：数据永不结束也不能超时（原先写死 timeout=(5,12) 会拖到 17s）"""
        slow = asyncio.run_coroutine_threadsafe(
            asyncio.start_server(_slow_trickle, "127.0.0.1", 0), self.loop).result(timeout=10)
        slow_port = slow.sockets[0].getsockname()[1]
        try:
            proxy = parse_one(f"http://127.0.0.1:{slow_port}")
            node = DownloadNode("local", "本机", "http://node.local/endless")

            t0 = time.monotonic()
            res = download(proxy, [node], 10 ** 9, multi=1, hard_limit=2.0)
            wall = time.monotonic() - t0

            self.assertLessEqual(wall, 3.0)            # curl --max-time 2s 真的断开了
            self.assertTrue(res["ok"], res)            # 断开不影响已传部分出速率
            self.assertGreater(res["bytes"], 0)
            self.assertLessEqual(res["ms"], 3_000)
        finally:
            self.loop.call_soon_threadsafe(slow.close)


class TestTcpPing(unittest.TestCase):
    """TCP 连通探测：地址族交给 getaddrinfo，硬编码 AF_INET 会把 IPv6 代理误判为不通"""

    def _listen(self, family: int, addr: tuple) -> int:
        """在回环上起监听，返回端口；本机不支持该地址族时跳过"""
        try:
            srv = socket.socket(family, socket.SOCK_STREAM)
            srv.bind(addr)
        except OSError:
            self.skipTest(f"本机不支持 {addr} 监听")
        srv.listen(1)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def test_ipv4_listener_is_connected(self):
        port = self._listen(socket.AF_INET, ("127.0.0.1", 0))
        res = tcp_ping(parse_one(f"http://127.0.0.1:{port}"), timeout=2.0)
        self.assertTrue(res["connected"])
        self.assertIsNotNone(res["latency_ms"])

    def test_ipv6_literal_is_connected(self):
        port = self._listen(socket.AF_INET6, ("::1", 0))
        res = tcp_ping(parse_one(f"http://[::1]:{port}"), timeout=2.0)
        self.assertTrue(res["connected"])
        self.assertIsNotNone(res["latency_ms"])

    def test_closed_port_is_not_connected(self):
        res = tcp_ping(parse_one("http://127.0.0.1:1"), timeout=1.0)
        self.assertFalse(res["connected"])
        self.assertIsNone(res["latency_ms"])


class TestCliNodeErrors(unittest.TestCase):
    """CLI 传错节点名必须报错退出，不能静默跑出"通过但没速度"的假结果"""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = cli_main(argv)
        return code, err.getvalue()

    def test_unknown_download_node_exits_2(self):
        code, err = self._run(["--proxy", "http://1.2.3.4:8080", "--nodes", "typo"])
        self.assertEqual(code, 2)
        self.assertIn("typo", err)

    def test_unknown_upload_node_exits_2(self):
        code, err = self._run(["--proxy", "http://1.2.3.4:8080", "--test", "upload",
                               "--upload-nodes", "typo"])
        self.assertEqual(code, 2)
        self.assertIn("typo", err)

    def test_non_positive_timeout_exits_2(self):
        """--timeout 0 会被 curl 理解为不限时长，必须在入口拦住"""
        code, err = self._run(["--proxy", "http://1.2.3.4:8080", "--timeout", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--timeout", err)


if __name__ == "__main__":
    unittest.main()
