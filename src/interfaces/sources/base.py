"""HTTP API 代理源公共基类：URL 构建、请求、重试骨架

BASE_URL / DEFAULT_PARAMS / RETRIES / TIMEOUT 类属性与 _parse 由子类提供。
PROTOCOL 为代理协议（http/https/socks5），入池前统一加上 scheme 前缀。
geonode 因分页逻辑覆盖 fetch_proxies，其余源直接用基类默认实现。
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from src.interfaces import http_client

logger = logging.getLogger(__name__)


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

    def _fetch_until_nonempty(self, url: str, retries=None, timeout=None) -> list[str]:
        """重试循环拉取，首次非空即返回；全部失败返回空列表"""
        max_retries = retries if retries is not None else self.retries
        req_timeout = timeout if timeout is not None else self.timeout
        for _ in range(max_retries):
            try:
                resp = self._request(url, timeout=req_timeout)
                resp.raise_for_status()
                proxies = self._parse(resp)
                if proxies:
                    logger.info("请求成功: %s, 获取 %d 个代理", url, len(proxies))
                    return proxies
            except Exception as e:
                logger.warning("请求失败: %s", e)
        return []

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
