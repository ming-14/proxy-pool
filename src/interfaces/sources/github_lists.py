"""GitHub 静态代理列表源（proxifly / monosans / clarketm / ALIILAPRO / hookzof）

这些仓库只提供纯文本列表（每行一个代理），没有 base_url/params 语义，故不套用
HttpSourceFetcher。一次拉取多条列表并合并，由拉取用例按 scheme 分拣入
github-http / github-https / github-socks5 组。

raw.githubusercontent.com 在部分网络直连被 SSL 中断，镜像前缀由环境变量
GITHUB_LISTS_MIRROR 提供（未设置或为空即直连，代码不内嵌任何镜像地址）。
单条列表失败只记日志并跳过，不影响其它列表——与拉取用例「单源失败不中断」同一口径。
"""

from __future__ import annotations

import logging
import os
import re

from src.domain.models import SUPPORTED_SCHEMES
from src.domain.ports import ProxySource
from src.interfaces import http_client

logger = logging.getLogger(__name__)

MIRROR_ENV = "GITHUB_LISTS_MIRROR"
_RAW_ROOT = "https://raw.githubusercontent.com/"

# (raw 路径, 列表协议)：条目自带 scheme 时以条目为准，裸 ip:port 用列表协议补全。
# clarketm 的列表无协议字段（实测存活的均为 HTTP），按 http 处理；
# proxifly 的 socks5 与 hookzof 的列表同源同内容，只保留后者避免重复下载。
LISTS: tuple[tuple[str, str], ...] = (
    (f"{_RAW_ROOT}proxifly/free-proxy-list/main/proxies/protocols/http/data.txt", "http"),
    (f"{_RAW_ROOT}proxifly/free-proxy-list/main/proxies/protocols/https/data.txt", "https"),
    (f"{_RAW_ROOT}monosans/proxy-list/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}clarketm/proxy-list/master/proxy-list-raw.txt", "http"),
    (f"{_RAW_ROOT}ALIILAPRO/Proxy/main/http.txt", "http"),
    (f"{_RAW_ROOT}ALIILAPRO/Proxy/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}hookzof/socks5_list/master/proxy.txt", "socks5"),
)

# 裸地址：IPv4/域名 + 端口，或 [IPv6]:端口
_BIND_RE = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[0-9A-Za-z][0-9A-Za-z.\-]*):\d{1,5}$")


def _mirror_prefix() -> str:
    """镜像前缀；未设置或为空串时直连"""
    return (os.environ.get(MIRROR_ENV) or "").strip()


def parse_line(line: str, protocol: str) -> str | None:
    """把一行文本规范化为代理 URL；空行/注释/格式非法返回 None

    条目自带受支持的 scheme 时以条目为准（列表可能混入其它协议），
    裸 ip:port 用列表协议补全。
    """
    entry = line.strip()
    if not entry or entry.startswith("#"):
        return None
    scheme, sep, bind = entry.partition("://")
    if not sep:
        scheme, bind = protocol, entry
    scheme = scheme.lower()
    if scheme not in SUPPORTED_SCHEMES or not _BIND_RE.match(bind):
        return None
    return f"{scheme}://{bind}"


class GithubListsFetcher:
    """多仓库静态列表代理源（ProxySource 端口实现）"""

    RETRIES = 2
    TIMEOUT = 15

    def __init__(self, *, lists=LISTS, mirror: str | None = None,
                 retries: int | None = None, timeout: int | None = None) -> None:
        """mirror=None 时读环境变量；其余参数缺省用类属性"""
        self._lists = tuple(lists)
        self._mirror = _mirror_prefix() if mirror is None else mirror
        self.retries = self.RETRIES if retries is None else retries
        self.timeout = self.TIMEOUT if timeout is None else timeout

    def fetch_proxies(self, retries=None, timeout=None) -> list[str]:
        """逐列表拉取并合并（跨列表去重），返回带 scheme 前缀的代理 URL 列表"""
        max_retries = self.retries if retries is None else retries
        req_timeout = self.timeout if timeout is None else timeout
        merged: dict[str, None] = {}
        for path, protocol in self._lists:
            for proxy in self._fetch_one(self._mirror + path, protocol, max_retries, req_timeout):
                merged.setdefault(proxy, None)
        logger.info("GitHub 列表源共获取 %d 个代理（%d 个列表）", len(merged), len(self._lists))
        return list(merged)

    def _fetch_one(self, url: str, protocol: str, retries: int, timeout: int) -> list[str]:
        """拉取单条列表并解析；重试直到拿到非空结果，全失败返回空列表"""
        for attempt in range(retries):
            try:
                resp = http_client.get(url, timeout=timeout)
                resp.raise_for_status()
                proxies = [p for p in (parse_line(line, protocol) for line in resp.text.splitlines()) if p]
                if proxies:
                    logger.info("列表 %s 获取 %d 个代理 (%s)", url, len(proxies), protocol)
                    return proxies
                logger.warning("列表 %s 无有效代理（第 %d/%d 次）", url, attempt + 1, retries)
            except Exception as e:
                logger.warning("列表 %s 请求失败（第 %d/%d 次）: %s", url, attempt + 1, retries, e)
        return []


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return GithubListsFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for item in GithubListsFetcher().fetch_proxies():
        print(item)
