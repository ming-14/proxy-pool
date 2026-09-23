import asyncio, socket, urllib.parse, time, re, base64, hmac, struct, hashlib, io, os

# 转发通道/握手/drain 的空闲超时基准（server.py 同值）。转发通道读空闲仅
# 在整会话双向静默满此值时回收（SessionActivity），握手与 drain 按单向超时。
SOCKET_TIMEOUT = 60

HTTP_LINE = re.compile('([^ ]+) +(.+?) +(HTTP/[^ ]+)$')
packstr = lambda s, n=1: len(s).to_bytes(n, 'big') + s

def netloc_split(loc, default_host=None, default_port=None):
    ipv6 = re.fullmatch(r'\[([0-9a-fA-F:]*)\](?::(\d+)?)?', loc)
    if ipv6:
        host_name, port = ipv6.groups()
    elif ':' in loc:
        host_name, port = loc.rsplit(':', 1)
    else:
        host_name, port = loc, None
    return host_name or default_host, int(port) if port else default_port

async def socks_address_stream(reader, n):
    if n in (1, 17):
        data = await reader.read_n(4)
        host_name = socket.inet_ntoa(data)
    elif n in (3, 19):
        data = await reader.read_n(1)
        data += await reader.read_n(data[0])
        host_name = data[1:].decode()
    elif n in (4, 20):
        data = await reader.read_n(16)
        host_name = socket.inet_ntop(socket.AF_INET6, data)
    else:
        raise Exception(f'Unknown address header {n}')
    data_port = await reader.read_n(2)
    return host_name, int.from_bytes(data_port, 'big'), data+data_port

def socks_address(reader, n):
    return socket.inet_ntoa(reader.read(4)) if n == 1 else \
           reader.read(reader.read(1)[0]).decode() if n == 3 else \
           socket.inet_ntop(socket.AF_INET6, reader.read(16)), \
           int.from_bytes(reader.read(2), 'big')

class SessionActivity:
    """一次转发的会话双向活动时间戳（stream_handler 创建，两条通道共享）。

    任一方向通道收到字节即 touch()；idle() 为整个会话的双向静默时长。
    转发通道的单向 read 空闲超时不再直接判定会话死亡——流式下载/SSE/长轮询中
    client→upstream 方向长期静默是完全正常的（旧语义下这类连接必然在
    SOCKET_TIMEOUT 处被误杀）；只有双向同时静默满 SOCKET_TIMEOUT（真僵尸
    keep-alive / 上游挂死）才回收会话，原有的内存/连接回收语义不变。
    """
    __slots__ = ("_last",)

    def __init__(self):
        self._last = time.monotonic()

    def touch(self):
        self._last = time.monotonic()

    def idle(self):
        return time.monotonic() - self._last


async def read_session(reader, activity, n=65536):
    """转发通道专用读：本方向空闲超时时按「整会话静默时长」决定是否回收。

    返回 b'' 表示通道应结束（对端 EOF 或双向共同静默满 SOCKET_TIMEOUT）。
    activity=None 时保持旧语义（单向空闲即结束，供 echo/empty 等特殊通道用）。
    StreamReader.read 被取消时数据仍留在内部缓冲，重试无丢失。
    """
    while True:
        try:
            # 与 read_w 同值的单向空闲节拍；asyncio.timeout(3.11+) 取消语义
            # 正确（同 drain 处注释），且空闲方向每 SOCKET_TIMEOUT 才醒一次，
            # 无轮询开销
            async with asyncio.timeout(SOCKET_TIMEOUT):
                data = await reader.read(n)
        except TimeoutError:
            if activity is None or activity.idle() >= SOCKET_TIMEOUT:
                return b''
            continue
        if data and activity is not None:
            activity.touch()
        return data


class BaseProtocol:
    def __init__(self, param):
        self.param = param
    @property
    def name(self):
        return self.__class__.__name__.lower()
    def reuse(self):
        return False
    def udp_accept(self, data, **kw):
        raise Exception(f'{self.name} don\'t support UDP server')
    def udp_connect(self, rauth, host_name, port, data, **kw):
        raise Exception(f'{self.name} don\'t support UDP client')
    def udp_unpack(self, data):
        return data
    def udp_pack(self, host_name, port, data):
        return data
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        raise Exception(f'{self.name} don\'t support client')
    async def channel(self, reader, writer, stat_bytes, stat_conn, activity=None):
        try:
            stat_conn(1)
            while not reader.at_eof() and not writer.is_closing():
                # 读用 read_session：本方向空闲仅在整会话双向静默超时后才结束通道，
                # 保住流式长连接；僵尸回收语义不变（见 SessionActivity）
                data = await read_session(reader, activity)
                if not data:
                    break
                if stat_bytes is None:
                    continue
                stat_bytes(len(data))
                writer.write(data)
                # asyncio.timeout（3.11+）：超时抛 TimeoutError 且正确等待被取消的 drain 协程，
                # 避免 wait_for 取消产生 "coroutine never awaited" 警告
                async with asyncio.timeout(SOCKET_TIMEOUT):
                    await writer.drain()
        except Exception:
            pass
        finally:
            stat_conn(-1)
            writer.close()

class Direct(BaseProtocol):
    pass

class Trojan(BaseProtocol):
    async def guess(self, reader, users, **kw):
        header = await reader.read_w(56)
        if users:
            for user in users:
                if hashlib.sha224(user).hexdigest().encode() == header:
                    return user
        else:
            if hashlib.sha224(b'').hexdigest().encode() == header:
                return True
        reader.rollback(header)
    async def accept(self, reader, user, **kw):
        assert await reader.read_n(2) == b'\x0d\x0a'
        if (await reader.read_n(1))[0] != 1:
            raise Exception('Connection closed')
        host_name, port, _ = await socks_address_stream(reader, (await reader.read_n(1))[0])
        assert await reader.read_n(2) == b'\x0d\x0a'
        return user, host_name, port
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        toauth = hashlib.sha224(rauth or b'').hexdigest().encode()
        writer_remote.write(toauth + b'\x0d\x0a\x01\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + b'\x0d\x0a')

class SSR(BaseProtocol):
    async def guess(self, reader, users, **kw):
        if users:
            header = await reader.read_w(max(len(i) for i in users))
            reader.rollback(header)
            user = next(filter(lambda x: x == header[:len(x)], users), None)
            if user is None:
                return
            await reader.read_n(len(user))
            return user
        header = await reader.read_w(1)
        reader.rollback(header)
        return header[0] in (1, 3, 4, 17, 19, 20)
    async def accept(self, reader, user, **kw):
        host_name, port, data = await socks_address_stream(reader, (await reader.read_n(1))[0])
        return user, host_name, port
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write(rauth + b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))

class SS(SSR):
    def patch_ota_reader(self, cipher, reader):
        chunk_id, data_len, _buffer = 0, None, bytearray()
        def decrypt(s):
            nonlocal chunk_id, data_len
            _buffer.extend(s)
            ret = bytearray()
            while 1:
                if data_len is None:
                    if len(_buffer) < 2:
                        break
                    data_len = int.from_bytes(_buffer[:2], 'big')
                    del _buffer[:2]
                else:
                    if len(_buffer) < 10+data_len:
                        break
                    data = _buffer[10:10+data_len]
                    assert _buffer[:10] == hmac.new(cipher.iv+chunk_id.to_bytes(4, 'big'), data, hashlib.sha1).digest()[:10]
                    del _buffer[:10+data_len]
                    data_len = None
                    chunk_id += 1
                    ret.extend(data)
            return bytes(ret)
        reader.decrypts.append(decrypt)
        if reader._buffer:
            reader._buffer = bytearray(decrypt(reader._buffer))
    def patch_ota_writer(self, cipher, writer):
        chunk_id = 0
        def write(data, o=writer.write):
            nonlocal chunk_id
            if not data: return
            checksum = hmac.new(cipher.iv+chunk_id.to_bytes(4, 'big'), data, hashlib.sha1).digest()
            chunk_id += 1
            return o(len(data).to_bytes(2, 'big') + checksum[:10] + data)
        writer.write = write
    async def accept(self, reader, user, reader_cipher, **kw):
        header = await reader.read_n(1)
        ota = (header[0] & 0x10 == 0x10)
        host_name, port, data = await socks_address_stream(reader, header[0])
        assert ota or not reader_cipher or not reader_cipher.ota, 'SS client must support OTA'
        if ota and reader_cipher:
            checksum = hmac.new(reader_cipher.iv+reader_cipher.key, header+data, hashlib.sha1).digest()
            assert checksum[:10] == await reader.read_n(10), 'Unknown OTA checksum'
            self.patch_ota_reader(reader_cipher, reader)
        return user, host_name, port
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, writer_cipher_r, **kw):
        writer_remote.write(rauth)
        if writer_cipher_r and writer_cipher_r.ota:
            rdata = b'\x13' + packstr(host_name.encode()) + port.to_bytes(2, 'big')
            checksum = hmac.new(writer_cipher_r.iv+writer_cipher_r.key, rdata, hashlib.sha1).digest()
            writer_remote.write(rdata + checksum[:10])
            self.patch_ota_writer(writer_cipher_r, writer_remote)
        else:
            writer_remote.write(b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))
    def udp_accept(self, data, users, **kw):
        reader = io.BytesIO(data)
        user = True
        if users:
            user = next(filter(lambda i: data[:len(i)]==i, users), None)
            if user is None:
                return
            reader.read(len(user))
        n = reader.read(1)[0]
        if n not in (1, 3, 4):
            return
        host_name, port = socks_address(reader, n)
        return user, host_name, port, reader.read()
    def udp_unpack(self, data):
        reader = io.BytesIO(data)
        n = reader.read(1)[0]
        host_name, port = socks_address(reader, n)
        return reader.read()
    def udp_pack(self, host_name, port, data):
        try:
            return b'\x01' + socket.inet_aton(host_name) + port.to_bytes(2, 'big') + data
        except Exception:
            pass
        return b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + data
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return rauth + b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + data

class Socks4(BaseProtocol):
    async def guess(self, reader, **kw):
        header = await reader.read_w(1)
        if header == b'\x04':
            return True
        reader.rollback(header)
    async def accept(self, reader, user, writer, users, authtable, **kw):
        assert await reader.read_n(1) == b'\x01'
        port = int.from_bytes(await reader.read_n(2), 'big')
        ip = await reader.read_n(4)
        userid = (await reader.read_until(b'\x00'))[:-1]
        user = authtable.authed()
        if users:
            if userid in users:
                user = userid
            elif not user:
                raise Exception(f'Unauthorized SOCKS {userid}')
            authtable.set_authed(user)
        # 成功应答推迟到上游隧道建立后（stream_handler 调 client_connected 时），
        # 与 Socks5 同模式：原实现在 accept 里先回 granted，之后无论目标连接是否
        # 成功客户端都只看到"已授权"，失败表现为"授权后立刻断连"（无失败码可回）。
        async def client_connected(writer_remote):
            writer.write(b'\x00\x5a' + port.to_bytes(2, 'big') + ip)
            await writer.drain()
        return user, socket.inet_ntoa(ip), port, client_connected
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        ip = socket.inet_aton((await asyncio.get_running_loop().getaddrinfo(host_name, port, family=socket.AF_INET))[0][4][0])
        writer_remote.write(b'\x04\x01' + port.to_bytes(2, 'big') + ip + rauth + b'\x00')
        assert await reader_remote.read_n(2) == b'\x00\x5a'
        await reader_remote.read_n(6)
    async def error_reply(self, writer, ex):
        """SOCKS4 失败应答：VN=0, CD=0x5B（request rejected or failed），后续 6 字节为
        目的地址（失败时无意义，置零）。原实现无此方法 → 连接失败/无可用上游时裸断连，
        客户端只能看到连接中断而非明确失败（SOCKS5 有 REP 码、HTTP 有 502，此处对齐）。
        """
        writer.write(b'\x00\x5b' + b'\x00' * 6)
        try:
            await writer.drain()
        except Exception:
            pass

class Socks5(BaseProtocol):
    async def guess(self, reader, **kw):
        header = await reader.read_w(1)
        if header == b'\x05':
            return True
        reader.rollback(header)
    async def accept(self, reader, user, writer, users, authtable, **kw):
        methods = await reader.read_n((await reader.read_n(1))[0])
        user = authtable.authed()
        if users and (not user or b'\x00' not in methods):
            if b'\x02' not in methods:
                # RFC 1928 §3：无可接受认证方法时回 05 FF 再关闭
                try:
                    writer.write(b'\x05\xff')
                    await writer.drain()
                except Exception:
                    pass
                raise Exception(f'Unauthorized SOCKS')
            writer.write(b'\x05\x02')
            assert (await reader.read_n(1))[0] == 1, 'Unknown SOCKS auth'
            u = await reader.read_n((await reader.read_n(1))[0])
            p = await reader.read_n((await reader.read_n(1))[0])
            user = u+b':'+p
            if user not in users:
                # RFC 1929 §2：认证失败回 01 01 再关闭
                try:
                    writer.write(b'\x01\x01')
                    await writer.drain()
                except Exception:
                    pass
                raise Exception(f'Unauthorized SOCKS {u}:{p}')
            writer.write(b'\x01\x00')
        elif users and not user:
            try:
                writer.write(b'\x05\xff')
                await writer.drain()
            except Exception:
                pass
            raise Exception(f'Unauthorized SOCKS')
        else:
            writer.write(b'\x05\x00')
        if users:
            authtable.set_authed(user)
        head = await reader.read_n(3)
        assert head[0] == 0x05, 'Unknown SOCKS protocol'
        if head[1] == 0x03:
            # UDP ASSOCIATE (RFC 1928 §7)：回复 UDP 中继地址，保持 TCP 连接直到客户端关闭。
            # 数据面由 -ul 端口的 datagram_handler 处理（与 TCP 连接无关联）。
            atyp = await reader.read_n(1)
            await socks_address_stream(reader, atyp[0])
            relay = next((o for o in (kw.get('ulisten') or []) if getattr(o, 'port', None) is not None), None)
            sockname = writer.get_extra_info('sockname') or (None, None)
            if relay is None:
                # 未配置 UDP 中继（-ul）：回 0x07 command not supported
                writer.write(b'\x05\x07\x00' + b'\x01\x00\x00\x00\x00\x00\x00')
                try:
                    await writer.drain()
                except Exception:
                    pass
                raise Exception('UDP relay not configured')
            if relay.port:
                host = relay.host_name or sockname[0] or '127.0.0.1'
                port = relay.port
            else:
                host = sockname[0] or '127.0.0.1'
                port = sockname[1]
            if ':' in host:
                bnd = b'\x04' + socket.inet_pton(socket.AF_INET6, host) + port.to_bytes(2, 'big')
            else:
                bnd = b'\x01' + socket.inet_aton(host) + port.to_bytes(2, 'big')
            writer.write(b'\x05\x00\x00' + bnd)
            try:
                await writer.drain()
            except Exception:
                pass
            # 复用 'empty' 语义：channel 读取并丢弃数据，连接保持到客户端关闭
            return user, 'empty', 0
        assert head[1] == 0x01, 'Unknown SOCKS protocol'
        header = await reader.read_n(1)
        host_name, port, data = await socks_address_stream(reader, header[0])
        # #169：成功应答推迟到目标连接成功后（stream_handler 调 client_connected 时），
        # 连接失败时由 stream_handler 调 error_reply 回 RFC1928 错误码，
        # 否则 curl 对关闭端口收到空响应（Empty reply）而非 05 05 Connection refused。
        async def client_connected(writer_remote):
            writer.write(b'\x05\x00\x00' + header + data)
            await writer.drain()
        return user, host_name, port, client_connected
    async def error_reply(self, writer, ex):
        """RFC 1928 REP 错误应答：目标连接失败时回 05 <code> 00 ... 再关闭"""
        rep = 0x01  # general failure
        if isinstance(ex, ConnectionRefusedError):
            rep = 0x05  # connection refused
        elif isinstance(ex, (socket.gaierror, UnicodeError)):
            # DNS 解析失败：gaierror 为常规解析错误；UnicodeError 为 IDNA 编码失败
            # （非法域名标签如 "bad!!name" 会先在 getaddrinfo 内抛 UnicodeError）
            rep = 0x04  # host unreachable（DNS 解析失败）
        elif isinstance(ex, asyncio.TimeoutError):
            rep = 0x06  # TTL expired / 连接超时
        elif str(ex).startswith('BLOCK'):
            rep = 0x02  # connection not allowed by ruleset
        writer.write(b'\x05' + bytes([rep]) + b'\x00\x01\x00\x00\x00\x00\x00\x00')
        try:
            await writer.drain()
        except Exception:
            pass
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        if rauth:
            writer_remote.write(b'\x05\x01\x02')
            assert await reader_remote.read_n(2) == b'\x05\x02'
            writer_remote.write(b'\x01' + b''.join(packstr(i) for i in rauth.split(b':', 1)))
            assert await reader_remote.read_n(2) == b'\x01\x00', 'Unknown SOCKS auth'
        else:
            writer_remote.write(b'\x05\x01\x00')
            assert await reader_remote.read_n(2) == b'\x05\x00'
        writer_remote.write(b'\x05\x01\x00\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))
        assert await reader_remote.read_n(3) == b'\x05\x00\x00'
        header = (await reader_remote.read_n(1))[0]
        await reader_remote.read_n(6 if header == 1 else (18 if header == 4 else (await reader_remote.read_n(1))[0]+2))
    def udp_accept(self, data, **kw):
        reader = io.BytesIO(data)
        if reader.read(3) != b'\x00\x00\x00':
            return
        n = reader.read(1)[0]
        if n not in (1, 3, 4):
            return
        host_name, port = socks_address(reader, n)
        return True, host_name, port, reader.read()
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return b'\x00\x00\x00\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + data

class HTTP(BaseProtocol):
    async def guess(self, reader, **kw):
        header = await reader.read_w(4)
        reader.rollback(header)
        return header in (b'GET ', b'HEAD', b'POST', b'PUT ', b'DELE', b'CONN', b'OPTI', b'TRAC', b'PATC')
    async def accept(self, reader, user, writer, **kw):
        lines = await reader.read_until(b'\r\n\r\n')
        headers = lines[:-4].decode().split('\r\n')
        method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
        lines = '\r\n'.join(i for i in headers if not i.startswith('Proxy-'))
        headers = dict(i.split(': ', 1) for i in headers if ': ' in i)
        async def reply(code, message, body=None, wait=False):
            writer.write(message)
            if body:
                writer.write(body)
            if wait:
                await writer.drain()
        return await self.http_accept(user, method, path, None, ver, lines, headers.get('Host', ''), headers.get('Proxy-Authorization'), reply, **kw)
    async def http_accept(self, user, method, path, authority, ver, lines, host, pauth, reply, authtable, users, httpget=None, **kw):
        url = urllib.parse.urlparse(path)
        if method == 'GET' and not url.hostname:
            for path, text in (httpget.items() if httpget else ()):
                if path == url.path:
                    user = next(filter(lambda x: x.decode()==url.query, users), None) if users else True
                    if user:
                        if users:
                            authtable.set_authed(user)
                        if type(text) is str:
                            text = (text % dict(host=host)).encode()
                        await reply(200, f'{ver} 200 OK\r\nConnection: close\r\nContent-Type: text/plain\r\nCache-Control: max-age=900\r\nContent-Length: {len(text)}\r\n\r\n'.encode(), text, True)
                        raise Exception('Connection closed')
            # 非代理形式 GET（path-only）：回真实 404 响应再关闭，而不是裸断连
            await reply(404, f'{ver} 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode(), wait=True)
            raise Exception(f'404 {method} {url.path}')
        if users:
            user = authtable.authed()
            if not user:
                user = next(filter(lambda i: ('Basic '+base64.b64encode(i).decode()) == pauth, users), None)
                if user is None:
                    await reply(407, f'{ver} 407 Proxy Authentication Required\r\nConnection: close\r\nProxy-Authenticate: Basic realm="simple"\r\n\r\n'.encode(), wait=True)
                    raise Exception('Unauthorized HTTP')
            authtable.set_authed(user)
        if method == 'CONNECT':
            host_name, port = netloc_split(authority or path)
            return user, host_name, port, lambda writer: reply(200, f'{ver} 200 Connection established\r\nConnection: close\r\n\r\n'.encode())
        else:
            host_name, port = netloc_split(url.netloc or host, default_port=80)
            newpath = url._replace(netloc='', scheme='').geturl()
            async def connected(writer):
                writer.write(f'{method} {newpath} {ver}\r\n{lines}\r\n\r\n'.encode())
                return True
            return user, host_name, port, connected
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write(f'CONNECT {host_name}:{port} HTTP/1.1\r\nHost: {host_name}:{port}'.encode() + (b'\r\nProxy-Authorization: Basic '+base64.b64encode(rauth) if rauth else b'') + b'\r\n\r\n')
        resp = await reader_remote.read_until(b'\r\n\r\n')
        # 非 200 应答（上游拒绝/重定向，如仅放行 443 的代理对 80 回 302）时抛错：
        # 若当成功继续，头部已被 read_until 消费，残留 body 会被当响应转发，
        # 客户端收到缺状态行/头的残缺响应（"代理无法使用"）。抛错后由
        # stream_handler 走 error_reply 明确告知客户端失败。
        status = resp.split(b'\r\n', 1)[0]
        if not status.startswith(b'HTTP/') or b' 200 ' not in status:
            raise Exception(f'CONNECT {host_name}:{port} failed: {status.decode(errors="replace")}')

    async def error_reply(self, writer, ex):
        """上游 CONNECT 失败（拒绝/超时/连接错误）时回 502，避免客户端收空/残缺响应"""
        writer.write(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()

    async def http_channel(self, reader, writer, stat_bytes, stat_conn, activity=None):
        try:
            stat_conn(1)
            while not reader.at_eof() and not writer.is_closing():
                # 同 channel：读用 read_session（会话双向活动判定），流式响应期间
                # client 方向静默不再误杀会话，僵尸回收语义不变
                data = await read_session(reader, activity)
                if not data:
                    break
                if b'\r\n' in data and HTTP_LINE.match(data.split(b'\r\n', 1)[0].decode()):
                    if b'\r\n\r\n' not in data:
                        data += await reader.read_until(b'\r\n\r\n')
                    lines, data = data.split(b'\r\n\r\n', 1)
                    headers = lines.decode().split('\r\n')
                    method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
                    lines = '\r\n'.join(i for i in headers if not i.startswith('Proxy-'))
                    headers = dict(i.split(': ', 1) for i in headers if ': ' in i)
                    newpath = urllib.parse.urlparse(path)._replace(netloc='', scheme='').geturl()
                    data = f'{method} {newpath} {ver}\r\n{lines}\r\n\r\n'.encode() + data
                stat_bytes(len(data))
                writer.write(data)
                # 同 channel：drain 加 SOCKET_TIMEOUT 超时（asyncio.timeout 3.11+）
                async with asyncio.timeout(SOCKET_TIMEOUT):
                    await writer.drain()
        except Exception:
            pass
        finally:
            stat_conn(-1)
            writer.close()

class HTTPOnly(HTTP):
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, myhost, **kw):
        buffer = bytearray()
        HOST_NAME = re.compile('\r\nHost: ([^\r\n]+)\r\n', re.I)
        def write(data, o=writer_remote.write):
            if not data: return
            buffer.extend(data)
            pos = buffer.find(b'\r\n\r\n')
            if pos != -1:
                text = buffer[:pos].decode()
                header = HTTP_LINE.match(text.split('\r\n', 1)[0])
                host = HOST_NAME.search(text)
                if not header or not host:
                    writer_remote.close()
                    raise Exception('Unknown HTTP header for protocol HTTPOnly')
                method, path, ver = header.groups()
                data = f'{method} http://{host.group(1)}{path} {ver}\r\nHost: {host.group(1)}'.encode() + (b'\r\nProxy-Authorization: Basic '+base64.b64encode(rauth) if rauth else b'') + b'\r\n\r\n' + buffer[pos+4:]
                buffer.clear()
                return o(data)
        writer_remote.write = write

class H2(HTTP):
    async def guess(self, reader, **kw):
        return True
    async def accept(self, reader, user, writer, **kw):
        if not writer.headers.done():
            await writer.headers
        headers = writer.headers.result()
        headers = {i.decode().lower():j.decode() for i,j in headers}
        lines = '\r\n'.join(i for i in headers if not i.startswith('proxy-') and not i.startswith(':'))
        async def reply(code, message, body=None, wait=False):
            writer.send_headers(((':status', str(code)),))
            if body:
                writer.write(body)
            if wait:
                await writer.drain()
        return await self.http_accept(user, headers[':method'], headers[':path'], headers[':authority'], '2.0', lines, '', headers.get('proxy-authorization'), reply, **kw)
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, myhost, **kw):
        headers = [(':method', 'CONNECT'), (':scheme', 'https'), (':path', '/'),
                   (':authority', f'{host_name}:{port}')]
        if rauth:
            headers.append(('proxy-authorization', 'Basic '+base64.b64encode(rauth)))
        writer_remote.send_headers(headers)

class H3(H2):
    pass

class SSH(BaseProtocol):
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, myhost, **kw):
        pass

class Transparent(BaseProtocol):
    async def guess(self, reader, sock, **kw):
        remote = self.query_remote(sock)
        return remote is not None and (sock is None or sock.getsockname() != remote)
    async def accept(self, reader, user, sock, **kw):
        remote = self.query_remote(sock)
        return user, remote[0], remote[1]
    def udp_accept(self, data, sock, **kw):
        remote = self.query_remote(sock)
        return True, remote[0], remote[1], data

SO_ORIGINAL_DST = 80
SOL_IPV6 = 41
class Redir(Transparent):
    def query_remote(self, sock):
        try:
            #if sock.family == socket.AF_INET:
            if "." in sock.getsockname()[0]:
                buf = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
                assert len(buf) == 16
                return socket.inet_ntoa(buf[4:8]), int.from_bytes(buf[2:4], 'big')
            else:
                buf = sock.getsockopt(SOL_IPV6, SO_ORIGINAL_DST, 28)
                assert len(buf) == 28
                return socket.inet_ntop(socket.AF_INET6, buf[8:24]), int.from_bytes(buf[2:4], 'big')
        except Exception:
            pass

class Pf(Transparent):
    def query_remote(self, sock):
        try:
            import fcntl
            src = sock.getpeername()
            dst = sock.getsockname()
            src_ip = socket.inet_pton(sock.family, src[0])
            dst_ip = socket.inet_pton(sock.family, dst[0])
            pnl = bytearray(struct.pack('!16s16s32xHxxHxx8xBBxB', src_ip, dst_ip, src[1], dst[1], sock.family, socket.IPPROTO_TCP, 2))
            if not hasattr(self, 'pf'):
                self.pf = open('/dev/pf', 'a+b')
            fcntl.ioctl(self.pf.fileno(), 0xc0544417, pnl)
            return socket.inet_ntop(sock.family, pnl[48:48+len(src_ip)]), int.from_bytes(pnl[76:78], 'big')
        except Exception:
            pass

class Tunnel(Transparent):
    def query_remote(self, sock):
        if not self.param:
            return 'tunnel', 0
        dst = sock.getsockname() if sock else (None, None)
        return netloc_split(self.param, dst[0], dst[1])
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        pass
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return data

class WS(BaseProtocol):
    # RFC 6455 §1.3：Sec-WebSocket-Accept 计算用固定 GUID
    WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
    async def guess(self, reader, **kw):
        header = await reader.read_w(4)
        reader.rollback(header)
        return header == b'GET '
    def patch_ws_stream(self, reader, writer, masked=False):
        data_len, mask_key, _buffer = None, None, bytearray()
        def feed_data(s, o=reader.feed_data):
            nonlocal data_len, mask_key
            _buffer.extend(s)
            while 1:
                if data_len is None:
                    if len(_buffer) < 2:
                        break
                    required = 2 + (4 if _buffer[1]&128 else 0)
                    p = _buffer[1] & 127
                    required += 2 if p == 126 else 4 if p == 127 else 0
                    if len(_buffer) < required:
                        break
                    data_len = int.from_bytes(_buffer[2:4], 'big') if p == 126 else int.from_bytes(_buffer[2:6], 'big') if p == 127 else p
                    mask_key = _buffer[required-4:required] if _buffer[1]&128 else None
                    del _buffer[:required]
                else:
                    if len(_buffer) < data_len:
                        break
                    data = _buffer[:data_len]
                    if mask_key:
                        data = bytes(data[i]^mask_key[i%4] for i in range(data_len))
                    del _buffer[:data_len]
                    data_len = None
                    o(data)
        reader.feed_data = feed_data
        if reader._buffer:
            reader._buffer, buf = bytearray(), reader._buffer
            feed_data(buf)
        def write(data, o=writer.write):
            if not data: return
            data_len = len(data)
            if masked:
                mask_key = os.urandom(4)
                data = bytes(data[i]^mask_key[i%4] for i in range(data_len))
                return o(b'\x02' + (bytes([data_len|0x80]) if data_len < 126 else b'\xfe'+data_len.to_bytes(2, 'big') if data_len < 65536 else b'\xff'+data_len.to_bytes(4, 'big')) + mask_key + data)
            else:
                return o(b'\x02' + (bytes([data_len]) if data_len < 126 else b'\x7e'+data_len.to_bytes(2, 'big') if data_len < 65536 else b'\x7f'+data_len.to_bytes(4, 'big')) + data)
        writer.write = write
    async def accept(self, reader, user, writer, users, authtable, sock, **kw):
        lines = await reader.read_until(b'\r\n\r\n')
        headers = lines[:-4].decode().split('\r\n')
        method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
        lines = '\r\n'.join(i for i in headers if not i.startswith('Proxy-'))
        headers = dict(i.split(': ', 1) for i in headers if ': ' in i)
        url = urllib.parse.urlparse(path)
        if users:
            pauth = headers.get('Proxy-Authorization', None)
            user = authtable.authed()
            if not user:
                user = next(filter(lambda i: ('Basic '+base64.b64encode(i).decode()) == pauth, users), None)
                if user is None:
                    writer.write(f'{ver} 407 Proxy Authentication Required\r\nConnection: close\r\nProxy-Authenticate: Basic realm="simple"\r\n\r\n'.encode())
                    raise Exception('Unauthorized WebSocket')
            authtable.set_authed(user)
        if method != 'GET':
            raise Exception(f'Unsupported method {method}')
        if headers.get('Sec-WebSocket-Key', None) is None:
            raise Exception(f'Unsupported headers {headers}')
        seckey = base64.b64decode(headers.get('Sec-WebSocket-Key'))
        rseckey = base64.b64encode(hashlib.sha1(seckey + self.WS_GUID).digest()).decode()
        writer.write(f'{ver} 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {rseckey}\r\nSec-WebSocket-Protocol: chat\r\n\r\n'.encode())
        self.patch_ws_stream(reader, writer, False)
        if not self.param:
            return 'tunnel', 0
        dst = sock.getsockname()
        host, port = netloc_split(self.param, dst[0], dst[1])
        return user, host, port
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, myhost, **kw):
        seckey = base64.b64encode(os.urandom(16)).decode()
        writer_remote.write(f'GET / HTTP/1.1\r\nHost: {myhost}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {seckey}\r\nSec-WebSocket-Protocol: chat\r\nSec-WebSocket-Version: 13'.encode() + (b'\r\nProxy-Authorization: Basic '+base64.b64encode(rauth) if rauth else b'') + b'\r\n\r\n')
        await reader_remote.read_until(b'\r\n\r\n')
        self.patch_ws_stream(reader_remote, writer_remote, True)

class Echo(Transparent):
    def query_remote(self, sock):
        return 'echo', 0

async def accept(protos, reader, **kw):
    for proto in protos:
        try:
            user = await proto.guess(reader, **kw)
        except Exception:
            raise Exception('Connection closed')
        if user:
            ret = await proto.accept(reader, user, **kw)
            while len(ret) < 4:
                ret += (None,)
            return (proto,) + ret
    raise Exception(f'Unsupported protocol')

def udp_accept(protos, data, **kw):
    for proto in protos:
        ret = proto.udp_accept(data, **kw)
        if ret:
            return (proto,) + ret
    raise Exception(f'Unsupported protocol {data[:10]}')

MAPPINGS = dict(direct=Direct, http=HTTP, httponly=HTTPOnly, ssh=SSH, socks5=Socks5, socks4=Socks4, socks=Socks5, ss=SS, ssr=SSR, redir=Redir, pf=Pf, tunnel=Tunnel, echo=Echo, ws=WS, trojan=Trojan, h2=H2, h3=H3, ssl='', secure='', quic='')
MAPPINGS['in'] = ''

def get_protos(rawprotos):
    protos = []
    for s in rawprotos:
        s, _, param = s.partition('{')
        param = param[:-1] if param else None
        p = MAPPINGS.get(s)
        if p is None:
            return f'existing protocols: {list(MAPPINGS.keys())}', None
        if p and p not in protos:
            protos.append(p(param))
    if not protos:
        return 'no protocol specified', None
    return None, protos

def sslwrap(reader, writer, sslcontext, server_side=False, server_hostname=None, verbose=None):
    if sslcontext is None:
        return reader, writer
    ssl_reader = asyncio.StreamReader()
    class Protocol(asyncio.Protocol):
        def data_received(self, data):
            ssl_reader.feed_data(data)
        def eof_received(self):
            ssl_reader.feed_eof()
        def connection_lost(self, exc):
            ssl_reader.feed_eof()
    ssl = asyncio.sslproto.SSLProtocol(asyncio.get_running_loop(), Protocol(), sslcontext, None, server_side, server_hostname, False)
    class Transport(asyncio.Transport):
        _paused = False
        def __init__(self, extra={}):
            self._extra = extra
            self.closed = False
        def write(self, data):
            if data and not self.closed:
                writer.write(data)
        def close(self):
            self.closed = True
            writer.close()
        def _force_close(self, exc):
            if not self.closed and verbose is not None:
                # verbose=None 表示调用方不需要输出（sslwrap 作为客户端连上游时不传 verbose）。
                # 上游原写法 (verbose or print) 在 None 时退化为 print 直接刷 stdout，绕过调用方日志。
                verbose(f'{exc} from {writer.get_extra_info("peername")[0]}')
            ssl._app_transport._closed = True
            self.close()
        def abort(self):
            self.close()
    ssl.connection_made(Transport())
    async def channel():
        read_size=65536
        buffer=None
        if hasattr(ssl,'get_buffer'):
            buffer=ssl.get_buffer(read_size)
        try:
            while not reader.at_eof() and not ssl._app_transport._closed:
                data = await reader.read(read_size)
                if not data:
                    break
                if buffer!=None:
                    data_len=len(data)
                    buffer[:data_len]=data
                    ssl.buffer_updated(data_len)
                else:
                    ssl.data_received(data)
        except Exception:
            pass
        finally:
            ssl.eof_received()
    asyncio.ensure_future(channel())
    class Writer():
        def get_extra_info(self, key):
            return writer.get_extra_info(key)
        def write(self, data):
            ssl._app_transport.write(data)
        def drain(self):
            return writer.drain()
        def is_closing(self):
            return ssl._app_transport._closed
        def close(self):
            if not ssl._app_transport._closed:
                ssl._app_transport.close()
    return ssl_reader, Writer()
