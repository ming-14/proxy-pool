"""HProxy 免费代理 API（https://hproxy.com/api/proxy-list）

JSON 数组，每项 {ip, port, protocols: [...]}，单页 limit 条。
protocols 常混入 socks4（工程不支持）或同时给多种协议——一个代理只入池一条，
按 SCHEME_PRIORITY 取一个受支持协议，全不受支持的条目丢弃。
"""

from src.domain.models import pick_supported_scheme
from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class HproxyFetcher(HttpSourceFetcher):
    BASE_URL = "https://hproxy.com/api/proxy-list"
    DEFAULT_PARAMS = {"format": "json", "limit": "500", "offset": "0"}
    RETRIES = 3
    TIMEOUT = 15

    def _parse(self, resp) -> list[str]:
        data = resp.json()
        proxies = []
        for item in data if isinstance(data, list) else []:
            if not item.get("ip") or not item.get("port"):
                continue
            scheme = pick_supported_scheme(item.get("protocols") or ())
            if scheme:
                proxies.append(f"{scheme}://{item['ip']}:{item['port']}")
        return proxies


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return HproxyFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    proxies = HproxyFetcher().fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
