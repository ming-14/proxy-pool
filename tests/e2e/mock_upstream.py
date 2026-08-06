"""e2e 测试共享设施：本地 mock 代理上游（HTTP CONNECT + SOCKS5）

模拟一个真实代理服务器：HTTP CONNECT 隧道 + 普通请求 + SOCKS5 握手，
让 pproxy 转发到本地，不依赖外网，验证完整转发链路。
"""

import asyncio
import io
import logging

logger = logging.getLogger(__name__)

MOCK_BODY = b"mock-response-body"


class _ReplayReader:
    """把已读出的首字节放回流，模拟 StreamReader.read 的凑满语义"""

    def __init__(self, first_byte, inner):
        self._buf = io.BytesIO(first_byte)
        self._inner = inner

    async def read(self, n=-1):
        if n is None or n < 0:
            return await self._inner.read()
        b = self._buf.read(n)
        if b:
            if len(b) >= n:
                return b
            rest = await self._inner.read(n - len(b))
            return b + rest
        return await self._inner.read(n)


async def _handle_socks5(reader, writer):
    """SOCKS5 握手：版本协商 -> 连接请求 -> 应答成功，之后回传模拟响应"""
    greeting = await reader.read(2)
    if len(greeting) < 2 or greeting[0] != 0x05:
        return
    nmethods = greeting[1]
    await reader.read(nmethods)
    writer.write(b"\x05\x00")  # 选 NO AUTH
    await writer.drain()

    head = await reader.read(4)
    if len(head) < 4 or head[0] != 0x05:
        return
    atyp = head[3]
    if atyp == 0x01:
        await reader.read(4)
    elif atyp == 0x03:
        ln = (await reader.read(1))[0]
        await reader.read(ln)
    elif atyp == 0x04:
        await reader.read(16)
    else:
        return
    await reader.read(2)  # port

    writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success
    await writer.drain()

    data = await reader.read(1024)
    if data:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
            % (len(MOCK_BODY), MOCK_BODY)
        )
        await writer.drain()


async def _handle_http(reader, writer):
    """HTTP 代理：CONNECT 隧道 + 普通请求"""
    req = await reader.read(1024)
    if not req:
        return
    first_line = req.split(b"\r\n", 1)[0].decode(errors="replace")
    if first_line.startswith("CONNECT"):
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        data = await reader.read(1024)
        if data:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
                % (len(MOCK_BODY), MOCK_BODY)
            )
            await writer.drain()
    else:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
            % (len(MOCK_BODY), MOCK_BODY)
        )
        await writer.drain()


async def _handle_mock_upstream(reader, writer):
    """按首个字节判断协议：0x05 走 SOCKS5，否则走 HTTP"""
    try:
        first = await reader.read(1)
        if not first:
            return
        replay = _ReplayReader(first, reader)
        if first[0] == 0x05:
            await _handle_socks5(replay, writer)
        else:
            await _handle_http(replay, writer)
    except Exception as e:
        logger.warning("mock 上游异常: %s", e)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_mock_upstream(loop=None) -> tuple:
    """启动 mock 上游代理服务器，返回 (server, port)"""
    srv = await asyncio.start_server(_handle_mock_upstream, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port
