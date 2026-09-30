"""Geonode 代理源（https://proxylist.geonode.com/api/proxy-list）

JSON 分页接口，每页 limit 条，逐页拉取直到空页或总数达标。
"""

from __future__ import annotations

import logging

from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher, retry_fetch

logger = logging.getLogger(__name__)


class GeonodeProxyFetcher(HttpSourceFetcher):
    BASE_URL = "https://proxylist.geonode.com/api/proxy-list"
    DEFAULT_PARAMS = {"protocols": "http,https", "limit": "500", "sort_by": "responseTime", "sort_type": "asc"}
    RETRIES = 3
    TIMEOUT = 15

    def __init__(self, base_url=None, params=None, retries=None, timeout=None):
        super().__init__(base_url=base_url, params=params, retries=retries, timeout=timeout)

    def _parse(self, resp) -> list[str]:
        """从单页 JSON 响应解析 http 协议代理列表（ip:port）"""
        data = resp.json()
        items = data.get("data", [])
        return [
            f'{item["ip"]}:{item["port"]}'
            for item in items
            if item.get("ip") and item.get("port") and "http" in item.get("protocols", [])
        ]

    def fetch_proxies(self, retries=None, timeout=None) -> list[str]:
        """分页拉取代理，只保留 http 协议代理，跨页去重

        逐页拉取直到空页或单页不足 limit 条（分页结束）。末页空结果是正常终止，
        accept 直接接受空页（不触发无谓重试）；整页重试耗尽则按分页结束处理。
        """
        max_retries = retries if retries is not None else self.retries
        req_timeout = timeout if timeout is not None else self.timeout
        limit = int(self.params.get("limit", 500))

        all_proxies: list[str] = []
        seen: set[str] = set()
        page = 1
        while True:
            url = self._build_url({"page": str(page)})
            page_proxies = retry_fetch(
                lambda url=url: self._fetch_once(url, req_timeout),
                retries=max_retries, label=f"page={page}", accept=lambda _: True,
            )
            if page_proxies is None:
                logger.info("请求结束: 已拉取 %d 页, 获取 %d 个代理", page - 1, len(all_proxies))
                return self._with_protocol(all_proxies)
            if not page_proxies:
                logger.info("请求成功: 共拉取 %d 页, 获取 %d 个代理", page - 1, len(all_proxies))
                return self._with_protocol(all_proxies)
            for p in page_proxies:
                if p not in seen:
                    seen.add(p)
                    all_proxies.append(p)
            if len(page_proxies) < limit:
                logger.info("请求成功: 共拉取 %d 页, 获取 %d 个代理", page, len(all_proxies))
                return self._with_protocol(all_proxies)
            page += 1


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return GeonodeProxyFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    fetcher = GeonodeProxyFetcher()
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
