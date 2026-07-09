import logging
import os
import sys
import re
import requests
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log import setup_logging
setup_logging()

logger = logging.getLogger(__name__)

BASE_URL = "http://api.89ip.cn/tqdl.html"
DEFAULT_PARAMS = {"api": "1", "num": "60", "port": "", "address": "", "isp": ""}
RETRIES = 3
TIMEOUT = 10


class IP89ProxyFetcher:
    def __init__(self, base_url=None, params=None, num=None, retries=None, timeout=None):
        self.base_url = base_url or BASE_URL
        self.params = dict(params or DEFAULT_PARAMS)
        if num is not None:
            self.params["num"] = str(num)
        self.retries = retries if retries is not None else RETRIES
        self.timeout = timeout if timeout is not None else TIMEOUT

    def _build_url(self, extra_params=None):
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        return f"{self.base_url}?{urlencode(params)}"

    def fetch_proxies(self, retries=None, timeout=None):
        url = self._build_url()
        max_retries = retries if retries is not None else self.retries
        req_timeout = timeout if timeout is not None else self.timeout

        for _ in range(max_retries):
            try:
                resp = requests.get(url, timeout=req_timeout)
                resp.raise_for_status()
                proxies = re.findall(r'\d+\.\d+\.\d+\.\d+:\d+', resp.text)
                if proxies:
                    return proxies
            except Exception as e:
                logger.warning("请求失败: %s", e)
        return []


fetch_proxies = IP89ProxyFetcher().fetch_proxies

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(DEFAULT_PARAMS["num"])
    fetcher = IP89ProxyFetcher(num=n)
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
