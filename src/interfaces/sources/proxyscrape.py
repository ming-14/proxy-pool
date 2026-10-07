"""ProxyScrape 免费代理 API（https://api.proxyscrape.com/v4/free-proxy-list/get）

纯文本，`proxy_format=protocolipport` 时每行自带 scheme（http:// / socks5:// / socks4://），
协议由条目自身决定并据此归组；socks4 不在 SUPPORTED_SCHEMES 内，parse_line 返回 None 丢弃。
不带 `protocol` 参数即返回全部协议（约 2000 条）。
"""

from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher, parse_line


class ProxyScrapeFetcher(HttpSourceFetcher):
    BASE_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get"
    DEFAULT_PARAMS = {"request": "display_proxies", "proxy_format": "protocolipport", "format": "text"}
    RETRIES = 3
    TIMEOUT = 15

    def _parse(self, resp) -> list[str]:
        # 该格式恒带 scheme，兜底协议 http 只作用于异常出现的裸地址
        return [p for p in (parse_line(line, "http") for line in resp.text.splitlines()) if p]


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return ProxyScrapeFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    proxies = ProxyScrapeFetcher().fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
