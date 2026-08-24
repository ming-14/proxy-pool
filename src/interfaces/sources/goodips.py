"""Goodips 代理源（http://api.goodips.com/）

JSON 接口，code==0 时 data 为 [{ip, port}, ...]。
"""

from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class GoodipsProxyFetcher(HttpSourceFetcher):
    BASE_URL = "http://api.goodips.com/"
    DEFAULT_PARAMS = {"num": "50", "protocol": "HTTP", "format": "json"}
    RETRIES = 5
    TIMEOUT = 10

    def __init__(self, base_url=None, params=None, num=None, retries=None, timeout=None):
        super().__init__(base_url=base_url, params=params, retries=retries, timeout=timeout)
        if num is not None:
            self.params["num"] = str(num)

    def _parse(self, resp):
        data = resp.json()
        if data.get("code") == 0:
            return [f'{item["ip"]}:{item["port"]}' for item in data.get("data", [])]
        return []


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return GoodipsProxyFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(GoodipsProxyFetcher.DEFAULT_PARAMS["num"])
    fetcher = GoodipsProxyFetcher(num=n)
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
