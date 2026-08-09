"""出口 IP 地理定位：在线查询 + 进程内缓存。"""
from __future__ import annotations

import requests

_CACHE: dict[str, dict | None] = {}
_API = "https://ipinfo.io/{ip}/json"


def lookup(ip: str, timeout: float = 5.0) -> dict | None:
    if not ip:
        return None
    if ip in _CACHE:
        return _CACHE[ip]
    try:
        r = requests.get(_API.format(ip=ip), timeout=timeout,
                         headers={"User-Agent": "pspeed/1.0"})
        if r.status_code == 200:
            d = r.json()
            info = {
                "country": d.get("country"),
                "region": d.get("region"),
                "city": d.get("city"),
                "org": d.get("org"),
                "timezone": d.get("timezone"),
            }
            _CACHE[ip] = info
            return info
    except Exception:
        pass
    _CACHE[ip] = None
    return None
