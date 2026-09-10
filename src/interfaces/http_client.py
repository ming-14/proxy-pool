"""HTTP 请求端口（HttpGetter）实现

所有出站 HTTP 请求统一走模块级 get()，底层用 requests（无 TLS 指纹随机化）。
明文 HTTP 请求失败时按 DNS 全部 A 记录用 IP+Host 头遍历重试（规避部分节点
"TCP 通但 HTTP 层被 RST"导致的整体失败）。

线程级 Session 复用：asyncio.to_thread 线程池中并发调用，requests.Session
非线程安全（连接池并发访问导致 C 层内存泄漏）。用 threading.local 为每个
线程创建独立 Session（线程池 8 线程 = 8 个 Session，不增长），既复用
SSLContext 避免反复加载证书库，又避免并发竞争。

AsyncHttpGetter：原生 aiohttp async 客户端，SSL 在事件循环管理（不经线程池），
避免 ssl 模块在 asyncio.to_thread 中的 C 层内存泄漏。供 check_proxies（心跳探测）
和 network_gate（网络门控）使用——这两个是高频并发调用场景。

返回 duck-typed 响应（status_code/text/headers/encoding/json/raise_for_status），
用例层只读这些属性，不 import 本模块实现。
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:  # 仅类型标注引用，运行时不导入
    import requests as _requests_mod

logger = logging.getLogger(__name__)

# requests 链惰性加载：serve 稳态（心跳/门控/补测）全走 aiohttp，requests 仅
# fetch/purity/CLI request 用到——顶层导入会让 web-off serve 白载 ~4.5MB 边际内存。
# disable_warnings 副作用（抑制 verify=False 刷屏）必须随导入同处执行。
_requests = None


def _load_requests():
    global _requests
    if _requests is None:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _requests = requests
    return _requests


# 线程级 Session：每个线程独立 Session（线程安全），复用 SSLContext（避免 C 层泄漏）
_local = threading.local()


def _get_session() -> _requests_mod.Session:
    """获取当前线程的 Session（没有则创建，线程池线程不退出故 Session 只创建一次）"""
    s = getattr(_local, "session", None)
    if s is None:
        s = _load_requests().Session()
        _local.session = s
    return s


def _resolve_ips(host: str) -> list[str]:
    """解析域名全部 IPv4 地址（去重）"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def _replace_host(url: str, host: str, ip: str) -> str:
    """将 URL 中的域名替换为指定 IP，保留端口与路径"""
    parsed = urlparse(url)
    netloc = parsed.netloc.replace(host, ip, 1)
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def get(url, *, timeout=None, headers=None, proxies=None, verify=False):
    """GET 请求（所有出站请求统一入口，复用线程级 Session）

    不校验 SSL 证书（verify=False）：免费代理多自签证书，校验会误杀。
    明文 HTTP 请求失败时按 DNS 全部 A 记录用 IP+Host 头遍历重试。

    Args:
        url:     目标 URL
        timeout: 超时秒数
        headers: 自定义请求头
        proxies: 代理字典（{"http": ..., "https": ...}）
        verify:  是否校验 SSL 证书
    """
    session = _get_session()
    try:
        return session.get(url, timeout=timeout, headers=headers, proxies=proxies, verify=verify)
    except Exception:
        # 仅明文 HTTP、未走代理时按多 IP 遍历重试（代理由对方负责解析）
        if proxies:
            raise
        parsed = urlparse(url)
        host = parsed.hostname or ""
        ips = _resolve_ips(host)
        if not ips:
            raise
        logger.warning("%s 请求失败，遍历 DNS 多 IP 重试: %s", host, ips)
        host_headers = dict(headers or {})
        host_headers.setdefault("Host", host)
        last_exc = None
        for ip in ips:
            try:
                return session.get(
                    _replace_host(url, host, ip), timeout=timeout,
                    headers=host_headers, proxies=None, verify=verify,
                )
            except Exception as e:
                last_exc = e
        raise last_exc


class HttpGetter:
    """HttpGetter 端口实现（统一走 requests，不校验证书）"""

    def get(self, url, *, timeout, headers=None, proxies=None, verify=False):
        return get(url, timeout=timeout, headers=headers, proxies=proxies, verify=verify)


# ------------------------------------------------------------------ async
import aiohttp
from aiohttp_socks import ProxyConnector


class _AsyncResponse:
    """aiohttp 响应的轻量包装（只暴露 status_code，do_test 只读此属性）"""
    __slots__ = ("status_code",)

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class AsyncHttpGetter:
    """原生 async HTTP 请求（aiohttp），SSL 在事件循环管理，不经线程池

    供 check_proxies（心跳探测）和 network_gate（网络门控）使用——
    高频并发场景下避免 requests + asyncio.to_thread 的 ssl C 层内存泄漏。

    http/https 代理：复用模块级 ClientSession（aiohttp async 安全，可跨协程共享），
    通过 proxy= 参数指定代理。
    socks5 代理：aiohttp 不原生支持，用 aiohttp_socks.ProxyConnector 创建临时 session
    （socks5 代理数少，临时 session 开销可忽略）。
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取模块级 ClientSession（延迟创建，须在事件循环中调用）"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get(self, url: str, *, timeout: float, headers: dict | None = None,
                  proxy: str | None = None, verify: bool = False) -> _AsyncResponse:
        """async GET 请求

        Args:
            proxy:  代理 URL 字符串（"http://..." / "socks5://..."），None 直连
            verify: 是否校验 SSL 证书
        """
        # aiohttp ssl 语义：True=校验证书, False=不校验, None=默认校验(=True)。
        # verify=False 时须传 ssl=False（None 仍校验，免费代理自签证书会握手失败）。
        ssl_ctx = True if verify else False
        aio_timeout = aiohttp.ClientTimeout(total=timeout)
        hdrs = headers or {}

        if proxy and proxy.startswith("socks5://"):
            # socks5：aiohttp 不原生支持，用 ProxyConnector 创建临时 session
            connector = ProxyConnector.from_url(proxy)
            async with aiohttp.ClientSession(connector=connector, timeout=aio_timeout) as sess:
                async with sess.get(url, headers=hdrs, ssl=ssl_ctx) as resp:
                    return _AsyncResponse(resp.status)
        else:
            # http/https 代理或直连：复用共享 session
            sess = await self._get_session()
            async with sess.get(url, headers=hdrs, proxy=proxy, ssl=ssl_ctx,
                                timeout=aio_timeout) as resp:
                return _AsyncResponse(resp.status)

    async def close(self) -> None:
        """关闭共享 session（serve 退出时调用）"""
        if self._session and not self._session.closed:
            await self._session.close()
