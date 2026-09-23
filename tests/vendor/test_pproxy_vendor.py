"""vendor/pproxy 本地维护回归测试

覆盖上游 repo (qwj/python-proxy) 已确认的 bug，逐一内化修复并回归：
- #179: pproxy-2.7.9 回归，CLI `-l http+in://...` 报
  `'ProxyBackward' object has no attribute 'sockets'`
  （print_server_started 遍历 server.sockets，但 ProxyBackward.start_server 返回自身）
- #204: `-v` 下 backward 隧道转发报 `'ProxyBackward' object is not subscriptable`
  （verbose.modstat 第 4 参被 vendor 化 stream_handler 改为传 roption，
  roption 被误作 stats → stats[0] 下标失败，且 modstat 在 try 外抛异常
  导致已建立的 writer_remote 不关闭 → CLOSE_WAIT 泄漏）
- #161: Python 3.11 起 SSLProtocol 变 BufferedProtocol，http+ssl:// 上游
  TLS 握手需 get_buffer/buffer_updated（PR #163 已内化，本测试防回归）
- 流式长连接误杀：转发通道单向读空闲超时（SOCKET_TIMEOUT）即结束会话，
  流式响应期间 client→upstream 天然静默 → 超过窗口的连接必被掐。
  修复后会话回收以「双向共同静默满窗口」（proto.SessionActivity）为准（本测试防回归）
- #169 补漏：目标连接**超时**曾单独一个 `except asyncio.TimeoutError` 分支只 `raise`，
  绕过 `error_reply` → 超时场景客户端仍收空响应。修复后超时与其它连接失败统一回错误应答
  （HTTP 502 / Socks5 REP 0x06），见 `TestConnectTimeoutErrorReply`（本测试防回归）
- no-direct：`schedule` 返回 None 时 `or DIRECT` 使空/全挂上游的请求以本机真实 IP
  直连目标（静默泄漏，客户端拿到的还是成功响应）。修复：handler 支持
  `direct_fallback=False`（本项目默认关闭），无可用上游时按协议回错误，
  见 `TestNoDirectFallback`（本测试防回归）
- SOCKS4 应答时序：`Socks4.accept` 原立即回 `00 5A`，失败码永远发不出去
  （失败=授权后立刻断连）。修复：成功应答推迟到隧道建立后 + 失败回 `00 5B`，
  见 `TestSocks4ReplyDeferred`（本测试防回归）
"""

import asyncio
import datetime
import http.server
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    # cryptography 缺失时全部置 None，_make_self_signed_cert 入口统一跳过 TLS 用例
    x509 = None
    hashes = None
    serialization = None
    rsa = None
    NameOID = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pproxy  # noqa: E402

from src.interfaces.proxy_server import PproxyServer  # noqa: E402


def _free_port() -> int:
    """获取一个当前可用的本地端口（bind 0 探测后释放，测试串行无竞争）"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_port(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> bool:
    """轮询 TCP 端口直到可连接（服务就绪）；超时返回 False"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            r, w = await asyncio.open_connection(host, port)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return True
        except OSError:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)


async def _wait_file_marker(path: str, marker: bytes, timeout: float = 10.0) -> str:
    """轮询文件内容直到包含 marker；返回文件全文（超时返回已读部分）"""
    deadline = time.time() + timeout
    data = b""
    while time.time() < deadline:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if marker in data:
                break
        except OSError:
            pass
        await asyncio.sleep(0.1)
    return data.decode(errors="replace")


def _make_self_signed_cert(directory):
    """生成临时自签名证书 (cert_path, key_path)，供 TLS 测试用"""
    if rsa is None:  # cryptography 缺失：TLS 用例跳过（环境依赖，非产物回归）
        raise unittest.SkipTest("cryptography 未安装，跳过 TLS 用例")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"localhost")])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class TestPrintServerStartedBackward(unittest.TestCase):
    """#179: print_server_started 对 ProxyBackward（无 sockets 属性）不应抛异常"""

    def test_backward_listen_prints_without_sockets(self):
        # ProxyBackward.start_server 返回自身（虚拟 server），无 .sockets 属性
        backward = pproxy.server.proxies_by_uri("http+in://127.0.0.1:18081")
        prints = []
        pproxy.server.print_server_started(backward, backward, lambda option, bind=None: prints.append(bind))
        self.assertIsNone(prints[0])


class TestBackwardListenCli(unittest.TestCase):
    """#179 e2e：`python -m pproxy -l http+in://...` 应正常启动，不报 sockets 错误"""

    def test_backward_listen_cli_starts(self):
        port = _free_port()
        out_fd, out_path = tempfile.mkstemp(prefix="pproxy-test-")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "pproxy", "-l", f"http+in://127.0.0.1:{port}", "-v"],
            cwd=VENDOR_DIR,
            stdout=out_fd,
            stderr=subprocess.STDOUT,
        )
        os.close(out_fd)
        try:
            output = asyncio.run(_wait_file_marker(out_path, b"Serving on"))
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
            os.unlink(out_path)
        self.assertNotIn("has no attribute 'sockets'", output, output)
        self.assertNotIn("Start server failed", output, output)
        self.assertIn("Serving on", output, output)


class TestBackwardTunnelVerbose(unittest.TestCase):
    """#204 e2e：public + behind 双 pproxy 隧道，`-v` 下请求应正常且无连接泄漏

    旧行为：verbose.modstat 收到 roption 后 `stats[0]` 抛
    'ProxyBackward' object is not subscriptable → 客户端收空响应，
    已建立的 writer_remote 不关闭 → CLOSE_WAIT 单调增长。
    """

    def test_backward_tunnel_verbose_no_leak(self):
        async def run():
            port_mock, port_public, port_tunnel = _free_port(), _free_port(), _free_port()
            responses = []

            async def mock_handle(reader, writer):
                try:
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            break
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            await asyncio.start_server(mock_handle, "127.0.0.1", port_mock)
            out_files = []
            procs = []
            try:
                for cmd in (
                    [sys.executable, "-u", "-m", "pproxy",
                     "-l", f"http+in://127.0.0.1:{port_tunnel}",
                     "-r", f"http://127.0.0.1:{port_mock}", "-v"],
                    [sys.executable, "-u", "-m", "pproxy",
                     "-l", f"http://127.0.0.1:{port_public}",
                     "-r", f"http+in://127.0.0.1:{port_tunnel}", "-v"],
                ):
                    fd, path = tempfile.mkstemp(prefix="pproxy-test-")
                    out_files.append((fd, path))
                    procs.append(subprocess.Popen(cmd, cwd=VENDOR_DIR, stdout=fd,
                                                  stderr=subprocess.STDOUT))
                    os.close(fd)
                behind, public = procs
                # 等 public 监听就绪，再重试请求直到隧道可用（behind 连上需要时间）
                assert await _wait_port(port_public), "public 代理未就绪"
                for _ in range(40):
                    reader, writer = await asyncio.open_connection("127.0.0.1", port_public)
                    writer.write(b"GET http://target.test/test HTTP/1.1\r\n"
                                 b"Host: target.test\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    try:
                        data = await asyncio.wait_for(reader.read(65536), timeout=2)
                    except asyncio.TimeoutError:
                        data = b""
                    responses.append(data)
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                    if b"ok" in data:
                        break
                    await asyncio.sleep(0.2)
                await asyncio.sleep(0.5)
            finally:
                for p in procs:
                    p.terminate()
                try:
                    for p in procs:
                        await asyncio.wait_for(asyncio.to_thread(p.wait, 5), timeout=8)
                except Exception:
                    for p in procs:
                        p.kill()
                out = ""
                for fd, path in out_files:
                    try:
                        with open(path, "rb") as f:
                            out += f.read().decode(errors="replace")
                    except OSError:
                        pass
                    os.unlink(path)
            return responses, out

        responses, out = asyncio.run(run())
        self.assertTrue(any(b"ok" in r for r in responses),
                        f"响应应包含 ok: {[r[:30] for r in responses]}\n{out}")
        self.assertNotIn("not subscriptable", out, out)
        self.assertNotIn("Traceback", out, out)


class TestMainEventLoopCompat(unittest.TestCase):
    """#200/#205: Python 3.14 下 main() 顶层 get_event_loop() 抛 RuntimeError

    修复：main() 改用 asyncio.new_event_loop() + set_event_loop（--test 分支同样），
    async 上下文内的 get_event_loop() 全部换成 get_running_loop()。
    e2e：CLI 能正常启动（main 主 loop），--test 分支能跑通。
    """

    def test_main_starts_with_verbose(self):
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "pproxy",
             "-l", f"http://127.0.0.1:{port}", "-v"],
            cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(asyncio.run(_wait_port(port)), "代理服务未就绪")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            output = proc.stdout.read().decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertIn("Serving on", output, output)
        self.assertNotIn("Traceback", output, output)
        self.assertNotIn("RuntimeError", output, output)

    def test_test_url_branch(self):
        # --test 分支：main() 里 new_event_loop().run_until_complete(test_url(...))
        # 目标端口不监听时也应正常退出，而非 get_event_loop RuntimeError
        port = _free_port()  # 未监听端口，连接立即被拒
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "pproxy",
             "--test", f"http://127.0.0.1:{port}/x",
             "-r", f"http://127.0.0.1:{port}"],
            cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = proc.communicate(timeout=30)
            output = output.decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertNotIn("RuntimeError", output, output)
        self.assertNotIn("get_event_loop", output, output)


class TestProxiesByUriUnderscore(unittest.TestCase):
    """#203: 跳转链分隔符 __ 不应误切 fragment auth 用户名里的合法 __

    pproxy URI 用 __ 连接跳转链（scheme://a__scheme://b），但 fragment auth
    用户名可合法包含 __（如 user__name:pass）。修复：分割时只把含 :// 的段
    视为独立跳转，被切开的用户名段合并回前一段。
    """

    def test_fragment_username_with_double_underscore(self):
        conn = pproxy.Connection("http://1.2.3.4:8080#user__name:pass")
        self.assertEqual(conn.users, [b"user__name:pass"])

    def test_jump_chain_still_works(self):
        # 正常跳转链不受影响
        conn = pproxy.Connection("ss://aes-256-cfb:key@1.2.3.4:1324__http://4.5.6.7:4321")
        self.assertEqual(conn.jump.host_name, "4.5.6.7")
        self.assertEqual(conn.host_name, "1.2.3.4")

    def test_jump_chain_with_underscore_username(self):
        # 跳转链 + fragment 用户名双下划线：用户名保留，跳转仍生效
        conn = pproxy.Connection("http://1.2.3.4:8080#user__name:pass__socks5://2.3.4.5:1080")
        self.assertEqual(conn.users, [b"user__name:pass"])
        self.assertEqual(conn.jump.host_name, "2.3.4.5")

    def test_single_underscore_username(self):
        # #193：单下划线/点用户名在 fragment 中本就支持（问题在 netloc 用法）
        conn = pproxy.Connection("http://1.2.3.4:8080#my.user_name:pass")
        self.assertEqual(conn.users, [b"my.user_name:pass"])


class TestSyntaxWarning(unittest.TestCase):
    """#196: Python 3.12+ 对无效转义序列报 SyntaxWarning

    修复：proto.py netloc_split 正则字符串加 r 前缀（原 '\\[' 为无效转义）。
    """

    def test_no_invalid_escape_in_proto(self):
        # -W error::SyntaxWarning 编译：有无效转义会抛 SyntaxError 使测试失败
        # 用环境变量传路径，避免 -c 字符串转义
        env = dict(os.environ, PPROXY_PROTO=os.path.join(VENDOR_DIR, "pproxy", "proto.py"))
        proc = subprocess.run(
            [sys.executable, "-W", "error::SyntaxWarning", "-c",
             "import ast, os; ast.parse(open(os.environ['PPROXY_PROTO'], encoding='utf-8').read())"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_netloc_split_ipv6_still_works(self):
        from pproxy.proto import netloc_split
        self.assertEqual(netloc_split("[::1]:8080"), ("::1", 8080))
        self.assertEqual(netloc_split("1.2.3.4:80"), ("1.2.3.4", 80))


@unittest.skipIf(x509 is None, "cryptography 未安装，跳过 TLS 相关测试")
class TestHttpSslUpstream(unittest.TestCase):
    """#161: http+ssl:// 上游（TLS HTTP 代理）在 3.11+ 上可正常转发

    Python 3.11 起 asyncio.sslproto.SSLProtocol 继承 BufferedProtocol，
    data_received 不再被调用，需 get_buffer/buffer_updated（PR #163）。
    e2e：本地代理 -r http+ssl://... 经 TLS CONNECT 隧道取回目标响应。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cert, key = _make_self_signed_cert(cls._tmp.name)

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"tls-target-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.target_port = _free_port()
        cls.tls_port = _free_port()
        cls.proxy_port = _free_port()
        t = threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
            ("127.0.0.1", cls.target_port), Target).serve_forever(), daemon=True)
        t.start()
        cls.cert, cls.key = cert, key

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    async def _run(self):
        sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sslctx.load_cert_chain(self.cert, self.key)

        async def tls_proxy_handle(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                line = header.split(b"\r\n")[0].decode()
                host, port = line.split()[1].rsplit(":", 1)
                r2, w2 = await asyncio.open_connection(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()

                async def pump(r, w):
                    try:
                        while True:
                            data = await r.read(65536)
                            if not data:
                                break
                            w.write(data)
                            await w.drain()
                    except Exception:
                        pass
                    finally:
                        try:
                            w.close()
                        except Exception:
                            pass

                await asyncio.gather(pump(reader, w2), pump(r2, writer))
            except Exception:
                try:
                    writer.close()
                except Exception:
                    pass

        tls_srv = await asyncio.start_server(tls_proxy_handle, "127.0.0.1", self.tls_port, ssl=sslctx)
        server = pproxy.Server(f"http://127.0.0.1:{self.proxy_port}")
        args = dict(rserver=[pproxy.Connection(f"http+ssl://127.0.0.1:{self.tls_port}")],
                    verbose=lambda *a: None, salgorithm="fa")
        handler = await server.start_server(args)
        try:
            assert await _wait_port(self.proxy_port), "代理服务未就绪"
            reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy_port)
            writer.write(b"GET http://127.0.0.1:%d/x HTTP/1.1\r\n"
                         b"Host: 127.0.0.1:%d\r\nConnection: close\r\n\r\n"
                         % (self.target_port, self.target_port))
            await writer.drain()
            data = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
                if not chunk:
                    break
                data += chunk
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return data
        finally:
            handler.close()
            await handler.wait_closed()
            tls_srv.close()
            await tls_srv.wait_closed()

    def test_tls_http_upstream_forward(self):
        data = asyncio.run(self._run())
        self.assertIn(b"200 OK", data)
        self.assertIn(b"tls-target-ok", data)


class TestHttpSslUpstreamReject(unittest.TestCase):
    """http+ssl 上游 CONNECT 被拒（非 200，如仅放行 443 的代理对 80 回 302）

    原行为：HTTP.connect 读 CONNECT 应答不验证状态码，302 被当成功——
    头部被 read_until 消费、残留 body 被当响应转发，客户端收到缺状态行的
    残缺响应。修复：非 200 抛错，stream_handler 经 HTTP.error_reply 回 502。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cert, key = _make_self_signed_cert(cls._tmp.name)
        cls.tls_port = _free_port()
        cls.proxy_port = _free_port()
        cls.cert, cls.key = cert, key

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    async def _run(self):
        sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sslctx.load_cert_chain(self.cert, self.key)

        async def reject_tunnel(reader, writer):
            # 模拟"仅放行 443"的代理: 对 CONNECT 回 302 + body 后关闭
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 302 Found\r\nServer: mock\r\n"
                             b"Content-Length: 13\r\n\r\nrejected-body")
                await writer.drain()
                writer.close()
            except Exception:
                try:
                    writer.close()
                except Exception:
                    pass

        tls_srv = await asyncio.start_server(reject_tunnel, "127.0.0.1", self.tls_port, ssl=sslctx)
        server = pproxy.Server(f"http://127.0.0.1:{self.proxy_port}")
        args = dict(rserver=[pproxy.Connection(f"http+ssl://127.0.0.1:{self.tls_port}")],
                    verbose=lambda *a: None, salgorithm="fa")
        handler = await server.start_server(args)
        try:
            assert await _wait_port(self.proxy_port), "代理服务未就绪"
            reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy_port)
            writer.write(b"GET http://example.com/ HTTP/1.1\r\n"
                         b"Host: example.com\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
                if not chunk:
                    break
                data += chunk
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return data
        finally:
            handler.close()
            await handler.wait_closed()
            tls_srv.close()
            await tls_srv.wait_closed()

    def test_rejected_connect_returns_502(self):
        """CONNECT 被拒时客户端收到完整 502，而不是残缺 302 body"""
        data = asyncio.run(self._run())
        self.assertIn(b"502 Bad Gateway", data)
        self.assertTrue(data.startswith(b"HTTP/"), "响应必须以 HTTP 状态行开头, 实际: %r" % data[:60])


class TestSocks5ErrorReply(unittest.TestCase):
    """#169: SOCKS5 目标连接失败应回 RFC1928 REP 错误码而非空响应

    原行为：Socks5.accept 收到 CONNECT 立即回 05 00（成功），目标端口关闭时
    stream_handler 只 close → curl 报 Empty reply（52）。修复：成功应答推迟到
    目标连接成功后，失败时回 05 05 Connection refused 等错误码。
    """

    def test_closed_port_returns_connection_refused(self):
        async def run():
            port_proxy, port_closed = _free_port(), _free_port()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "pproxy",
                 "-l", f"socks5://127.0.0.1:{port_proxy}", "-v"],
                cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                assert await _wait_port(port_proxy), "代理服务未就绪"
                reader, writer = await asyncio.open_connection("127.0.0.1", port_proxy)
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                await reader.readexactly(2)
                writer.write(b"\x05\x01\x00\x03" + bytes([9]) + b"127.0.0.1"
                             + port_closed.to_bytes(2, "big"))
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(10), timeout=5)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return resp
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        resp = asyncio.run(run())
        self.assertEqual(resp[0], 0x05)
        self.assertEqual(resp[1], 0x05, f"应为 Connection refused: {resp!r}")


class TestConnectTimeoutErrorReply(unittest.TestCase):
    """#169 补漏：目标连接**超时**同样要回明确错误，不能只回空响应

    旧实现把超时单独放一个 `except asyncio.TimeoutError` 分支且只 `raise`，绕过了
    `error_reply`——于是 proto 里 `Socks5.error_reply` 的 TimeoutError→0x06（TTL
    expired）与 `HTTP.error_reply` 的 502 全成死代码，客户端在超时场景仍拿到空响应
    （curl 报 Empty reply），与 #169「不再回空响应」的意图相悖。修复：超时与其它连接
    失败合并为一条分支统一回 `error_reply`，并把**原始异常**传下去——先包装成
    Exception 会丢类型，Socks5 的 REP 会退化成 0x01 general failure。

    `open_connection` 的超时默认值在 def 时求值（SOCKET_TIMEOUT=60s），patch 模块常量
    改不动它，故这里包一层把窗口压到 `TIMEOUT`，配合必然慢于窗口的 connect 复现超时。
    """

    TIMEOUT = 0.3

    async def _drive(self, listen_uri: str, drive):
        server = PproxyServer(f"{listen_uri}://127.0.0.1:0")
        # 显式 direct:// 上游：本项目默认不回退直连（no-direct），本用例要打的是
        # "上游连接卡住"的超时路径，故把直连作为**显式上游**放进去（等价于旧用例
        # 依赖的隐式回退，但不触发 no-direct 拒绝）
        await server.start(["direct://"])
        port = server._handler.sockets[0].getsockname()[1]
        try:
            return await drive(port)
        finally:
            await server.close()

    def _run(self, listen_uri: str, drive):
        timeout = self.TIMEOUT
        orig_open = pproxy.server.ProxyDirect.open_connection

        async def slow_connect(self, host, port, local_addr, family):
            await asyncio.sleep(5)      # 模拟 connect 卡住（SYN 无应答）
            raise OSError("unreachable")

        async def short_timeout_open(self, host, port, local_addr, lbind,
                                     _default=pproxy.server.SOCKET_TIMEOUT):
            return await orig_open(self, host, port, local_addr, lbind, timeout=timeout)

        with mock.patch.object(pproxy.server.ProxyDirect, "wait_open_connection", slow_connect), \
             mock.patch.object(pproxy.server.ProxyDirect, "open_connection", short_timeout_open):
            return asyncio.run(self._drive(listen_uri, drive))

    def test_http_connect_timeout_returns_502(self):
        """HTTP 代理：超时应回 502 Bad Gateway（旧行为：空响应）"""
        async def drive(port):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET http://203.0.113.9/ HTTP/1.1\r\nHost: 203.0.113.9\r\n\r\n")
            await writer.drain()
            buf = await asyncio.wait_for(reader.read(4096), timeout=10)
            writer.close()
            return buf

        buf = self._run("http", drive)
        self.assertTrue(buf.startswith(b"HTTP/1.1 502"), f"超时应回完整 502: {buf!r}")

    def test_socks5_connect_timeout_returns_ttl_expired(self):
        """SOCKS5：超时应回 REP 0x06 TTL expired（旧行为：空响应）"""
        async def drive(port):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            await reader.readexactly(2)                     # 05 00 no-auth
            writer.write(b"\x05\x01\x00\x03\x09" + b"127.0.0.1" + (80).to_bytes(2, "big"))
            await writer.drain()
            resp = await asyncio.wait_for(reader.readexactly(10), timeout=10)
            writer.close()
            return resp

        resp = self._run("socks5", drive)
        self.assertEqual(resp[0], 0x05)
        self.assertEqual(resp[1], 0x06, f"超时应回 TTL expired: {resp!r}")


class TestSocks5AuthReply(unittest.TestCase):
    """认证失败应答（RFC 1928 §3 / RFC 1929 §2）

    旧行为：无可接受认证方法 / 凭证错误时直接关闭连接，客户端只能看到连接中断；
    修复：先回 05 FF（no acceptable methods）或 01 01（auth failure）再关闭。
    """

    def test_no_auth_method_returns_05ff(self):
        async def run():
            port_proxy = _free_port()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "pproxy",
                 "-l", f"socks5://127.0.0.1:{port_proxy}#user:pass", "-v"],
                cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                assert await _wait_port(port_proxy), "代理服务未就绪"
                reader, writer = await asyncio.open_connection("127.0.0.1", port_proxy)
                # 仅提供 no-auth(0x00) 方法，服务端要求认证 -> 05 FF
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(2), timeout=5)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return resp
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        self.assertEqual(asyncio.run(run()), b"\x05\xff")

    def test_wrong_credentials_returns_0101(self):
        async def run():
            port_proxy = _free_port()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "pproxy",
                 "-l", f"socks5://127.0.0.1:{port_proxy}#user:pass", "-v"],
                cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                assert await _wait_port(port_proxy), "代理服务未就绪"
                reader, writer = await asyncio.open_connection("127.0.0.1", port_proxy)
                # 提供 user/pass(0x02) + no-auth(0x00) -> 服务端选 0x02
                writer.write(b"\x05\x02\x02\x00")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(2), timeout=5), b"\x05\x02")
                # RFC 1929 子协商：用户名 x / 密码 bad（错误凭证）-> 01 01
                writer.write(b"\x01\x01x\x03bad")
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(2), timeout=5)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return resp
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        self.assertEqual(asyncio.run(run()), b"\x01\x01")


class TestHttpPathOnlyRequest404(unittest.TestCase):
    """非代理形式请求（path-only GET）应回真实 404 而非裸断连

    旧行为：浏览器/curl 直接访问代理端口（非代理形式）时连接被直接关闭；
    修复：回 HTTP/1.1 404 Not Found（Connection: close）再关闭。
    """

    def test_path_only_get_returns_404(self):
        async def run():
            port_proxy = _free_port()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "pproxy",
                 "-l", f"http://127.0.0.1:{port_proxy}", "-v"],
                cwd=VENDOR_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                assert await _wait_port(port_proxy), "代理服务未就绪"
                reader, writer = await asyncio.open_connection("127.0.0.1", port_proxy)
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                await writer.drain()
                data = b""
                while True:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
                    if not chunk:
                        break
                    data += chunk
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return data
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        data = asyncio.run(run())
        self.assertIn(b"HTTP/1.1 404 Not Found", data, data)


class TestSessionIdleReclaim(unittest.TestCase):
    """转发通道空闲回收以「会话双向静默」为准（流式长连接误杀修复）

    旧行为（上游 pproxy 语义）：通道每次读套单向 SOCKET_TIMEOUT，流式下载/SSE
    期间 client→upstream 方向天然长期静默 → 超过窗口的响应必在超时点整会话掐断。
    修复：stream_handler 建 proto.SessionActivity 双向共享，任一方向有字节即刷新；
    通道单向读空闲仅在整会话静默满窗口时才结束（僵尸 keep-alive/上游挂死仍回收）。
    窗口 patch 为 2s 压缩测试耗时。
    """

    WINDOW = 2  # patch 后的空闲窗口（秒）

    async def _drive(self, mode: str, total_deadline: float):
        """mode: stream 慢速流式 / hang 收下请求永不响应 / quick 立即完整响应

        与 serve 相同的 PproxyServer 路径；显式 direct:// 上游（本项目默认不回退直连），
        只验证通道空闲语义。
        """
        async def origin_handler(reader, writer):
            await reader.read_until(b"\r\n\r\n")
            if mode == "quick":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                             b"Connection: close\r\n\r\nok")
                await writer.drain()
                writer.close()
                return
            if mode == "hang":
                await asyncio.sleep(60)  # 永不响应，等代理侧空闲回收
                writer.close()
                return
            # stream：chunked 流 10 帧共 ~4s（跨 2 个窗口），client 方向此时必然静默
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            for i in range(10):
                payload = f"data: tick {i}\n\n".encode()
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                await asyncio.sleep(0.4)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            writer.close()

        from src.interfaces.proxy_server import PproxyServer

        origin = await asyncio.start_server(lambda r, w: origin_handler(r, w), "127.0.0.1", 0)
        origin_port = origin.sockets[0].getsockname()[1]
        proxy_port = _free_port()
        server = PproxyServer(f"http://127.0.0.1:{proxy_port}")
        await server.start(["direct://"])  # 显式直连上游（no-direct 下唯一允许直连的途径）
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\n"
                         f"Host: 127.0.0.1:{origin_port}\r\n\r\n".encode())
            await writer.drain()
            t0 = time.monotonic()
            buf = bytearray()
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), timeout=total_deadline)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                buf.extend(data)
            closed_at = time.monotonic() - t0
            writer.close()
            return bytes(buf), closed_at
        finally:
            origin.close()
            await server.close()

    def _run(self, mode: str, total_deadline: float):
        # 两处 SOCKET_TIMEOUT 都必须 patch：pproxy.proto（read_session/drain 取值）
        # 与 pproxy.server（read_w 等猴子补丁方法按 server.py 全局取值——旧代码通道
        # 走 server.SOCKET_TIMEOUT，漏 patch 会让 stream 用例对旧代码失效）
        with mock.patch.object(pproxy.proto, "SOCKET_TIMEOUT", self.WINDOW), \
             mock.patch.object(pproxy.server, "SOCKET_TIMEOUT", self.WINDOW):
            return asyncio.run(self._drive(mode, total_deadline))

    def test_streaming_long_connection_survives(self):
        """流式响应期间 client 方向静默不得误杀会话（旧行为：t≈窗口处掐断）"""
        buf, _ = self._run("stream", total_deadline=30)
        self.assertIn(b"tick 9", buf, f"流被提前掐断，只收到 {len(buf)} 字节")
        self.assertTrue(buf.endswith(b"0\r\n\r\n"), "chunked 终止块缺失（响应不完整）")

    def test_zombie_session_still_reclaimed(self):
        """双向全静默（上游挂死不响应）仍在空闲窗口内回收——僵尸修复不回退"""
        buf, closed_at = self._run("hang", total_deadline=15)
        self.assertEqual(buf, b"", "hang 源站不应有任何响应字节")
        self.assertGreater(closed_at, 0.5, "回收过快会误伤刚建立的会话")
        self.assertLessEqual(closed_at, 4 * self.WINDOW,
                             f"僵尸会话未在空闲窗口内回收: {closed_at}s")

    def test_quick_request_unaffected(self):
        """普通短请求不受会话活动改造影响"""
        buf, _ = self._run("quick", total_deadline=10)
        self.assertIn(b"200 OK", buf)
        self.assertTrue(buf.endswith(b"ok"))


class TestNoDirectFallback(unittest.TestCase):
    """无可用上游时**不得回退直连**（本项目 direct_fallback=False）

    旧行为（上游 pproxy 语义）：schedule 返回 None 即 `or DIRECT`——上游列表空/全部
    不可用时，客户端请求会以**本机真实 IP 直连**目标，且返回成功响应，客户端无从察觉
    （为换出口 IP 而用代理的场景下这是静默 IP 泄漏）。修复：无可用上游时按协议回错误
    （HTTP 502 / SOCKS5 REP 0x01 / SOCKS4 CD 0x5B）并关闭；需要直连须显式配置 direct://。
    参数默认仍为 True（保留 pproxy CLI「-r 缺省即直连」的库语义），由 PproxyServer 显式关闭。
    """

    async def _serve_serve_path(self, upstreams: list[str]):
        """本项目 serve 路径（PproxyServer，恒 direct_fallback=False）→ (server, port)"""
        server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
        await server.start(upstreams)
        return server, server._handler.sockets[0].getsockname()[1]

    async def _serve_library_default(self):
        """pproxy 库默认路径（不经 PproxyServer，故不传 direct_fallback）→ (handler, port)"""
        server = pproxy.Server("http://127.0.0.1:0")
        handler = await server.start_server(dict(rserver=[], verbose=lambda *a: None, salgorithm="fa"))
        return handler, handler.sockets[0].getsockname()[1]

    async def _counting_target(self):
        """起一个"目标服务器"，记录是否被连上（直连泄漏的判据）"""
        hits = []

        async def handler(reader, writer):
            hits.append(1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        return srv, srv.sockets[0].getsockname()[1], hits

    def test_http_rejected_without_upstream_and_no_direct_connect(self):
        """空上游：回 502 且**从未**连过目标（旧行为：直连成功并回 200）"""
        async def run():
            target, target_port, hits = await self._counting_target()
            server, port = await self._serve_serve_path([])
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"GET http://127.0.0.1:{target_port}/ HTTP/1.1\r\n"
                             f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode())
                await writer.drain()
                buf = await asyncio.wait_for(reader.read(4096), timeout=10)
                writer.close()
            finally:
                await server.close()
                target.close()
            self.assertTrue(buf.startswith(b"HTTP/1.1 502"), f"应回 502: {buf!r}")
            self.assertEqual(hits, [], "无上游时不得直连目标（真实 IP 泄漏）")

        asyncio.run(run())

    def test_socks5_rejected_without_upstream(self):
        """空上游：SOCKS5 连接请求回 REP 0x01（general failure），而非直连成功"""
        async def run():
            server, port = await self._serve_serve_path([])
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                await reader.readexactly(2)          # 05 00 no-auth
                writer.write(b"\x05\x01\x00\x03\x09" + b"127.0.0.1" + (80).to_bytes(2, "big"))
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(10), timeout=10)
                writer.close()
            finally:
                await server.close()
            self.assertEqual(resp[0], 0x05)
            self.assertEqual(resp[1], 0x01, f"应为 general failure: {resp!r}")

        asyncio.run(run())

    def test_socks4_rejected_without_upstream(self):
        """空上游：SOCKS4 连接请求回 CD 0x5B（request rejected），而非裸断连"""
        async def run():
            server, port = await self._serve_serve_path([])
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"\x04\x01" + (80).to_bytes(2, "big") + b"\x7f\x00\x00\x01" + b"user\x00")
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(8), timeout=10)
                writer.close()
            finally:
                await server.close()
            self.assertEqual(resp[0], 0x00)
            self.assertEqual(resp[1], 0x5B, f"应为 request rejected: {resp!r}")

        asyncio.run(run())

    def test_library_default_still_falls_back_direct(self):
        """库语义保留：direct_fallback 缺省 True 时，空上游仍走直连（pproxy CLI -r 缺省行为）"""
        async def run():
            target, target_port, hits = await self._counting_target()
            handler, port = await self._serve_library_default()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"GET http://127.0.0.1:{target_port}/ HTTP/1.1\r\n"
                             f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode())
                await writer.drain()
                buf = await asyncio.wait_for(reader.read(4096), timeout=10)
                writer.close()
            finally:
                handler.close()
                await handler.wait_closed()
                target.close()
            self.assertIn(b"200 OK", buf)
            self.assertEqual(len(hits), 1, "缺省 direct_fallback=True 应仍走直连")

        asyncio.run(run())

    def test_udp_dropped_without_upstream(self):
        """UDP 路一致：无上游 + direct_fallback=False 时丢弃报文，不直连（UDP 无错误应答通道）"""
        async def run():
            sent = []

            class FakeWriter:
                def get_extra_info(self, key):
                    return None

                def sendto(self, data, addr):
                    sent.append((data, addr))

            payload = (b"\x00\x00\x00\x03\x09" + b"127.0.0.1"
                       + (53).to_bytes(2, "big") + b"q")
            await pproxy.server.datagram_handler(
                FakeWriter(), payload, ("127.0.0.1", 40000),
                [pproxy.proto.Socks5(None)], [], None, None, "rc", direct_fallback=False)
            self.assertEqual(sent, [], "无 UDP 上游时不得发出任何数据报（真实 IP 泄漏）")

        asyncio.run(run())

    def test_udp_direct_when_fallback_enabled(self):
        """门控确实起作用：缺省 direct_fallback=True 时 UDP 报文仍按直连发出"""
        async def run():
            got = []

            class Sink(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    got.append(data)

            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                Sink, local_addr=("127.0.0.1", 0))
            sink_port = transport.get_extra_info("socket").getsockname()[1]

            class FakeWriter:
                def get_extra_info(self, key):
                    return None

                def sendto(self, data, addr):
                    pass

            payload = (b"\x00\x00\x00\x03\x09" + b"127.0.0.1"
                       + sink_port.to_bytes(2, "big") + b"q")
            try:
                await pproxy.server.datagram_handler(
                    FakeWriter(), payload, ("127.0.0.1", 40000),
                    [pproxy.proto.Socks5(None)], [], None, None, "rc")
                await asyncio.sleep(0.5)
            finally:
                transport.close()
            self.assertEqual(got, [b"q"], f"缺省应走直连发出 payload: {got!r}")

        asyncio.run(run())


class TestSocks4ReplyDeferred(unittest.TestCase):
    """SOCKS4 成功应答推迟到上游隧道建立后（与 SOCKS5 #169 同模式）

    原行为：`Socks4.accept` 收到 CONNECT 立即回 `00 5A`（granted），之后无论目标
    连接是否成功，客户端都只看到"已授权"——失败表现为"授权后立刻断连"，失败码
    （`00 5B`）永远发不出去。修复：应答推迟到 `client_connected`，失败经
    `error_reply` 回 `00 5B`。
    """

    async def _serve(self):
        """显式 direct:// 上游（测握手时序，与 no-direct 语义无关）→ (server, port)"""
        server = PproxyServer("socks4://127.0.0.1:0")
        await server.start(["direct://"])
        return server, server._handler.sockets[0].getsockname()[1]

    def test_granted_only_after_tunnel_established(self):
        """目标可达时：拿到 granted 后转发正常（应答推迟不破坏成功路径）"""
        async def run():
            origin_hits = []

            async def origin_handler(reader, writer):
                origin_hits.append(1)
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
                writer.close()

            origin = await asyncio.start_server(origin_handler, "127.0.0.1", 0)
            origin_port = origin.sockets[0].getsockname()[1]
            server, port = await self._serve()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"\x04\x01" + origin_port.to_bytes(2, "big")
                             + b"\x7f\x00\x00\x01" + b"u\x00")
                await writer.drain()
                granted = await asyncio.wait_for(reader.readexactly(8), timeout=10)
                self.assertEqual(granted[:2], b"\x00\x5a", f"应回 granted: {granted!r}")
                writer.write(b"GET / HTTP/1.0\r\n\r\n")
                await writer.drain()
                data = b""
                while True:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                    if not chunk:
                        break
                    data += chunk
                writer.close()
            finally:
                await server.close()
                origin.close()
            self.assertEqual(len(origin_hits), 1, "应已连到目标")
            self.assertIn(b"ok", data)

        asyncio.run(run())

    def test_closed_target_returns_request_rejected(self):
        """目标不可达时：回 `00 5B`（旧行为：先回 granted 再断连）"""
        async def run():
            dead_port = _free_port()
            server, port = await self._serve()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"\x04\x01" + dead_port.to_bytes(2, "big")
                             + b"\x7f\x00\x00\x01" + b"u\x00")
                await writer.drain()
                resp = await asyncio.wait_for(reader.readexactly(8), timeout=10)
                writer.close()
            finally:
                await server.close()
            self.assertEqual(resp[:2], b"\x00\x5b", f"应为 request rejected: {resp!r}")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
