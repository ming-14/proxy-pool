"""Proxylister 免费代理 API（https://proxylister.com/api/v1/proxies）

JSON 分页结构 `{results: [{ip_address, port, protocols: [...]}], next, ...}`，单页 50 条。
protocols 常同时含 SOCKS4/HTTP/SOCKS5——一个代理只入池一条，按 SCHEME_PRIORITY
取一个受支持协议，全不受支持（纯 SOCKS4）的条目丢弃。
"""

from src.domain.models import pick_supported_scheme
from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class ProxylisterFetcher(HttpSourceFetcher):
    BASE_URL = "https://proxylister.com/api/v1/proxies"
    DEFAULT_PARAMS = {"limit": "50"}
    RETRIES = 3
    TIMEOUT = 15

    def _parse(self, resp) -> list[str]:
        data = resp.json()
        proxies = []
        for item in (data.get("results") or []):
            ip, port = item.get("ip_address"), item.get("port")
            if not ip or not port:
                continue
            scheme = pick_supported_scheme(item.get("protocols") or ())
            if scheme:
                proxies.append(f"{scheme}://{ip}:{port}")
        return proxies


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return ProxylisterFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    proxies = ProxylisterFetcher().fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
