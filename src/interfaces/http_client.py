"""HTTP 请求端口（HttpGetter）实现

所有出站 HTTP 请求统一走模块级 get()，底层用 requests（无 TLS 指纹随机化）。
明文 HTTP 请求失败时按 DNS 全部 A 记录用 IP+Host 头遍历重试（规避部分节点
"TCP 通但 HTTP 层被 RST"导致的整体失败）。

返回 duck-typed 响应（status_code/text/headers/encoding/json/raise_for_status），
用例层只读这些属性，不 import 本模块实现。
"""

import logging
import socket
from urllib.parse import urlparse, urlunparse

import requests as py_requests

logger = logging.getLogger(__name__)


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
    """GET 请求（所有出站请求统一入口）

    不校验 SSL 证书（verify=False）：免费代理多自签证书，校验会误杀。
    明文 HTTP 请求失败时按 DNS 全部 A 记录用 IP+Host 头遍历重试。

    Args:
        url:     目标 URL
        timeout: 超时秒数
        headers: 自定义请求头
        proxies: 代理字典（{"http": ..., "https": ...}）
        verify:  是否校验 SSL 证书
    """
    try:
        return py_requests.get(url, timeout=timeout, headers=headers, proxies=proxies, verify=verify)
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
                return py_requests.get(
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
