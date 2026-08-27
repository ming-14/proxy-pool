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

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    x509 = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

import pproxy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
