"""代理源公共设施：HTTP API 源基类（URL 构建 + 请求 + 解析）+ 全源共用重试策略 + 纯文本行解析

BASE_URL / DEFAULT_PARAMS / RETRIES / TIMEOUT 类属性与 _parse 由子类提供。
PROTOCOL 为代理协议（http/https/socks5），入池前统一加上 scheme 前缀。
geonode 因分页逻辑覆盖 fetch_proxies，其余源直接用基类默认实现。
parse_line 是纯文本列表源（github_lists / proxyscrape）共用的单行规范化入口。

retry_fetch / retry_fetch_async 是拉取源唯一的重试实现（日志口径一致，
尝试耗尽返回 None）：面向非 HttpSourceFetcher 子类的 github_lists 与
异步分阶段爬取的 freeproxy_world 同样复用，不各自写重试循环。
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlencode

from src.domain.models import SUPPORTED_SCHEMES
from src.interfaces import http_client

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_fetch(attempt: Callable[[], T], *, retries: int, label: str,
                accept: Callable[[T], bool] = bool) -> T | None:
    """重试 attempt()（一次完整尝试：请求 + 状态校验 + 解析），accept 通过即返回；尝试耗尽返回 None

    accept 缺省按非空判定（拉取直到有数据）；分页源的末页空结果是正常终止，
    传 accept=lambda _: True 立即接受。attempt 抛异常表示一次失败。
    """
    for attempt_no in range(retries):
        try:
            result = attempt()
        except Exception as e:
            logger.warning("%s 请求失败（第 %d/%d 次）: %s", label, attempt_no + 1, retries, e)
            continue
        if accept(result):
            return result
        logger.warning("%s 无有效数据（第 %d/%d 次）", label, attempt_no + 1, retries)
    return None


async def retry_fetch_async(attempt: Callable[[], Awaitable[T]], *, retries: int, label: str,
                            accept: Callable[[T], bool] = bool) -> T | None:
    """retry_fetch 的异步版（await attempt()，同一策略与日志口径）"""
    for attempt_no in range(retries):
        try:
            result = await attempt()
        except Exception as e:
            logger.warning("%s 请求失败（第 %d/%d 次）: %s", label, attempt_no + 1, retries, e)
            continue
        if accept(result):
            return result
        logger.warning("%s 无有效数据（第 %d/%d 次）", label, attempt_no + 1, retries)
    return None


# 裸地址：IPv4/域名 + 端口，或 [IPv6]:端口（github_lists 与 proxyscrape 等纯文本源共用）
BIND_RE = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[0-9A-Za-z][0-9A-Za-z.\-]*):\d{1,5}$")


def parse_line(line: str, protocol: str) -> str | None:
    """把纯文本列表的一行规范化为代理 URL；空行/注释/格式非法返回 None

    条目自带受支持的 scheme 时以条目为准（列表可能混入其它协议），裸 ip:port 用
    调用方给的列表协议补全；不受支持的协议（socks4 等）返回 None 由调用方丢弃。

    行内注释 `ip:port #国家` 与整行注释同口径：`#` 及其后忽略——代理 URL 不含 `#`，
    不剥离会让 `socks5://1.2.3.4:1080 #hk` 这类列表整份解析为 0 条。
    """
    entry = line.split("#", 1)[0].strip()
    if not entry:
        return None
    scheme, sep, bind = entry.partition("://")
    if not sep:
        scheme, bind = protocol, entry
    scheme = scheme.lower()
    if scheme not in SUPPORTED_SCHEMES or not BIND_RE.match(bind):
        return None
    return f"{scheme}://{bind}"


class HttpSourceFetcher:
    """通用 HTTP API 代理源基类"""

    BASE_URL: str = ""
    DEFAULT_PARAMS: dict = {}
    RETRIES: int = 3
    TIMEOUT: int = 10
    # 本源产出的代理协议（由请求参数隐含），入池时拼成 scheme://ip:port
    PROTOCOL: str = "http"

    def __init__(self, base_url=None, params=None, retries=None, timeout=None):
        self.base_url = base_url or self.BASE_URL
        self.params = dict(params or self.DEFAULT_PARAMS)
        self.retries = retries if retries is not None else self.RETRIES
        self.timeout = timeout if timeout is not None else self.TIMEOUT

    def _build_url(self, extra_params=None) -> str:
        """拼接请求 URL，extra_params 覆盖默认参数"""
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        return f"{self.base_url}?{urlencode(params)}"

    def _request(self, url: str, timeout=None):
        """发起 GET 请求（子类可覆盖以定制请求行为）"""
        return http_client.get(url, timeout=timeout)

    def _fetch_once(self, url: str, timeout: float) -> list[str]:
        """一次完整尝试：请求 + 状态校验 + 解析"""
        resp = self._request(url, timeout=timeout)
        resp.raise_for_status()
        return self._parse(resp)

    def _fetch_until_nonempty(self, url: str, retries=None, timeout=None) -> list[str]:
        """重试拉取，首次非空即返回；全部失败/全空返回空列表"""
        max_retries = retries if retries is not None else self.retries
        req_timeout = timeout if timeout is not None else self.timeout
        proxies = retry_fetch(lambda: self._fetch_once(url, req_timeout),
                              retries=max_retries, label=url)
        if proxies:
            logger.info("请求成功: %s, 获取 %d 个代理", url, len(proxies))
        return proxies or []

    def fetch_proxies(self, retries=None, timeout=None) -> list[str]:
        """拉取代理列表（单页模式），入池前补协议 scheme 前缀"""
        proxies = self._fetch_until_nonempty(self._build_url(), retries=retries, timeout=timeout)
        return self._with_protocol(proxies)

    @staticmethod
    def _with_protocol(proxies: list[str], protocol: str | None = None) -> list[str]:
        """给裸 ip:port 列表统一加 scheme 前缀（已带 scheme 的跳过）"""
        scheme = protocol or HttpSourceFetcher.PROTOCOL
        return [p if "://" in p else f"{scheme}://{p}" for p in proxies]

    def _parse(self, resp) -> list[str]:
        """从响应解析代理列表，由子类实现"""
        raise NotImplementedError
