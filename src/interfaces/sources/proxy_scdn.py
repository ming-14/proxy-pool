"""ProxyScdn 代理源（https://proxy.scdn.io/api/get_proxy.php）

JSON 接口，code==200 时 data.proxies 为代理地址列表。
"""

from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class ProxyScdnFetcher(HttpSourceFetcher):
    BASE_URL = "https://proxy.scdn.io/api/get_proxy.php"
    DEFAULT_PARAMS = {"protocol": "http", "count": "20"}
    RETRIES = 3
    TIMEOUT = 10

    def __init__(self, base_url=None, params=None, count=None, retries=None, timeout=None):
        super().__init__(base_url=base_url, params=params, retries=retries, timeout=timeout)
        if count is not None:
            self.params["count"] = str(count)

    def _parse(self, resp):
        data = resp.json()
        if data.get("code") == 200:
            return data["data"]["proxies"]
        return []


def create_source() -> ProxySource:
    """加载器入口：返回本源实例"""
    return ProxyScdnFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(ProxyScdnFetcher.DEFAULT_PARAMS["count"])
    fetcher = ProxyScdnFetcher(count=n)
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
