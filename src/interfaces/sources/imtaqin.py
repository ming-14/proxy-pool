"""imtaqin 免费代理 API（https://imtaqin.id/api/proxies）

JSON `{count, updated, protocols..., proxies: [{ip, port, protocol, ...}]}`，
单协议条目（协议名大写，如 "SOCKS5"），按 SCHEME_PRIORITY 归一后校验，SOCKS4 丢弃。
响应约 9MB / 5 万条且存量与其它源高度重合，sources.toml 为它配了 pause_ttl
限制拉取频率（否则默认 20 分钟一轮会反复下载 9MB）。
"""

from src.domain.models import pick_supported_scheme
from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class ImtaqinFetcher(HttpSourceFetcher):
    BASE_URL = "https://imtaqin.id/api/proxies"
    DEFAULT_PARAMS: dict = {}
    RETRIES = 3
    TIMEOUT = 30

    def _parse(self, resp) -> list[str]:
        data = resp.json()
        proxies = []
        for item in (data.get("proxies") or []):
            if not item.get("ip") or not item.get("port"):
                continue
            scheme = pick_supported_scheme((item.get("protocol"),))
            if scheme:
                proxies.append(f"{scheme}://{item['ip']}:{item['port']}")
        return proxies


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return ImtaqinFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    proxies = ImtaqinFetcher().fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
