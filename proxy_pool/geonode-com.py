import logging
import os
import sys
import requests
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log import setup_logging
setup_logging()

logger = logging.getLogger(__name__)

BASE_URL = "https://proxylist.geonode.com/api/proxy-list"
DEFAULT_PARAMS = {"protocols": "http,https", "limit": "500", "sort_by": "responseTime", "sort_type": "asc"}
RETRIES = 3
TIMEOUT = 15


class GeonodeProxyFetcher:
    def __init__(self, base_url=None, params=None, retries=None, timeout=None):
        self.base_url = base_url or BASE_URL
        self.params = dict(params or DEFAULT_PARAMS)
        self.retries = retries if retries is not None else RETRIES
        self.timeout = timeout if timeout is not None else TIMEOUT

    def _build_url(self, extra_params=None):
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        return f"{self.base_url}?{urlencode(params)}"

    def fetch_proxies(self, retries=None, timeout=None):
        max_retries = retries if retries is not None else self.retries
        req_timeout = timeout if timeout is not None else self.timeout

        all_proxies = []
        seen = set()
        page = 1
        while True:
            url = self._build_url({"page": str(page)})
            for _ in range(max_retries):
                try:
                    resp = requests.get(url, timeout=req_timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("data", [])
                    if not items:
                        return list(all_proxies)
                    for item in items:
                        ip = item.get("ip")
                        port = item.get("port")
                        protocols = item.get("protocols", [])
                        if ip and port and "http" in protocols:
                            p = f"{ip}:{port}"
                            if p not in seen:
                                seen.add(p)
                                all_proxies.append(p)
                    if len(items) < int(self.params.get("limit", 500)):
                        return list(all_proxies)
                    total = data.get("total", 0)
                    if total > 0 and len(seen) >= total:
                        return list(all_proxies)
                    page += 1
                    break
                except Exception as e:
                    logger.warning("请求失败 (page=%d): %s", page, e)
            else:
                return list(all_proxies)


fetch_proxies = GeonodeProxyFetcher().fetch_proxies

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    fetcher = GeonodeProxyFetcher()
    proxies = fetcher.fetch_proxies()
    for p in proxies:
        print(p)
    print(f"\n共 {len(proxies)} 个")
