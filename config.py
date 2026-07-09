import os

PROXY_GROUPS = {
    "default": 240,
    "freeproxy-world": 18000,
    "trusted": None,
    "blacklist": 10800,
}

MAX_FAILS = 2
TRUSTED_MAX_FAILS = 3

SOURCES = [
    {
        "name": "proxy_pool.proxy-scdn-io",
        "group": "default",
        "enabled": True,
        "pause_ttl": None,
    },
    {
        "name": "proxy_pool.89ip-cn",
        "group": "default",
        "enabled": True,
        "pause_ttl": None,
    },
    {
        "name": "proxy_pool.goodips-com",
        "group": "default",
        "enabled": True,
        "pause_ttl": None,
    },
    {
        "name": "proxy_pool.geonode-com",
        "group": "default",
        "enabled": True,
        "pause_ttl": None,
    },
    {
        "name": "proxy_pool.freeproxy-world",
        "group": "freeproxy-world",
        "enabled": True,
        "pause_ttl": 18000,
    },
]

SOURCE_PAUSE_TTL = 1200
SOURCE_DEDUP_THRESHOLD = 0.92
FETCH_PROXY_RETRIES = 3

FPW_MAX_CONCURRENT = 16
FPW_PROXY_RETRY = 1
FPW_TASK_INTERVAL = 2
FPW_REQUEST_TIMEOUT = 15

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
