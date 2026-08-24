"""89IP 免费代理源（http://api.89ip.cn/tqdl.html）

响应为纯文本，代理格式 ip:port，用正则提取。
"""

import re

from src.domain.ports import ProxySource
from src.interfaces.sources.base import HttpSourceFetcher


class IP89ProxyFetcher(HttpSourceFetcher):
    BASE_URL = "http://api.89ip.cn/tqdl.html"
    DEFAULT_PARAMS = {"api": "1", "num": "60", "port": "", "address": "", "isp": ""}
    RETRIES = 3
    TIMEOUT = 10

    def __init__(self, base_url=None, params=None, num=None, retries=None, timeout=None):
        super().__init__(base_url=base_url, params=params, retries=retries, timeout=timeout)
        if num is not None:
            self.params["num"] = str(num)

    def _parse(self, resp):
        return re.findall(r'\d+\.\d+\.\d+\.\d+:\d+', resp.text)


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return IP89ProxyFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(IP89ProxyFetcher.DEFAULT_PARAMS["num"])
    fetcher = IP89ProxyFetcher(num=n)
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
