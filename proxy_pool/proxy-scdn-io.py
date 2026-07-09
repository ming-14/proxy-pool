import logging
import os
import sys
import requests
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log import setup_logging
setup_logging()

logger = logging.getLogger(__name__)

BASE_URL = "https://proxy.scdn.io/api/get_proxy.php"
DEFAULT_PARAMS = {"protocol": "http", "count": "20"}
RETRIES = 3
TIMEOUT = 10


class ProxyScdnFetcher:
    def __init__(self, base_url=None, params=None, count=None, retries=None, timeout=None):
        self.base_url = base_url or BASE_URL
        self.params = dict(params or DEFAULT_PARAMS)
        if count is not None:
            self.params["count"] = str(count)
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
                data = resp.json()
                if data.get("code") == 200:
                    return data["data"]["proxies"]
            except Exception as e:
                logger.warning("请求失败: %s", e)
        return []


fetch_proxies = ProxyScdnFetcher().fetch_proxies

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(DEFAULT_PARAMS["count"])
    fetcher = ProxyScdnFetcher(count=n)
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
