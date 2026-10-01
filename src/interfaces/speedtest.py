"""pspeed 测速（domain.ports.ProxySpeedTester 实现）：Cloudflare 下载/上传测速 + 下载速率缓存

接口适配器层。vendor/pspeed（含 requests/PySocks 依赖链）仅在测速调用内 import，不为其
常驻加载；sys.path 注册是 stdlib 操作，留在模块级。

测速是 10s + 100MB 级操作，故：
- 全局串行锁：入队筛选的候选可能同时问测速，并发发起会打满带宽；
- download_mbps 按代理缓存 TTL（同一候选在缓存期内不重测，阈值判定过期重测即可），
  面板手动测速走 measure（用户点一次就真测一次，读缓存没有意义）。
"""

from __future__ import annotations

import os
import sys
import threading
import time

# 入队筛选的下载速率缓存时长秒
SPEEDTEST_CACHE_TTL = 3600.0

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PSPEED_SRC = os.path.join(_PROJECT_ROOT, "vendor", "pspeed", "src")
if _PSPEED_SRC not in sys.path:
    sys.path.insert(0, _PSPEED_SRC)


def _simplify(d: dict | None) -> dict | None:
    """精简 pspeed 返回的 download/upload dict，只保留前端需要的字段"""
    if not d:
        return None
    return {
        "ok": d.get("ok"),
        "best_mbps": d.get("best_mbps"),
        "avg_mbps": d.get("avg_mbps"),
        "bytes": d.get("bytes"),
        "ms": d.get("ms"),
        "node": d.get("node"),
        "err": d.get("err"),
    }


def _measure(proxy: str) -> dict:
    """同步调 pspeed probe_one：Cloudflare 下载+上传，10s 时限

    pspeed 及其依赖在此惰性导入（首次测速，~27ms）。
    """
    from pspeed.probe import probe_one
    from pspeed.proxy import parse_one
    from pspeed.nodes import get_download_nodes, get_upload_nodes

    px = parse_one(proxy)
    dl_nodes = get_download_nodes(["cloudflare"])
    ul_nodes = get_upload_nodes(["cloudflare"])
    result = probe_one(
        px, ["download", "upload"],
        dl_nodes=dl_nodes, ul_nodes=ul_nodes,
        dl_bytes=100 * 1024 * 1024,
        ul_bytes=5 * 1024 * 1024,
        multi=1, hard_limit=10.0,
    )
    return {
        "proxy": proxy,
        "download": _simplify(result.get("download")),
        "upload": _simplify(result.get("upload")),
    }


class PspeedTester:
    """pspeed Cloudflare 测速：面板完整结果 + 入队筛选下载速率（带缓存）"""

    def __init__(self, *, cache_ttl: float = SPEEDTEST_CACHE_TTL) -> None:
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, float]] = {}  # proxy -> (测速时刻, Mbps)
        self._lock = threading.Lock()  # 测速串行（10s + 100MB 级，不能并发）

    def measure(self, proxy: str) -> dict:
        """完整测速结果（面板测速端点用）"""
        with self._lock:
            return _measure(proxy)

    def download_mbps(self, proxy: str) -> float | None:
        """下载速率 Mbps；测不出（超时/失败/无速率）返回 None（判定层按未测到放过）"""
        cached = self._cache.get(proxy)
        if cached is not None and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]
        with self._lock:
            # 等锁期间同候选可能已被测过（fill 并发回调），复检避免重测；
            # 写缓存也在锁内，否则等锁者在写缓存前抢到锁会再测一次
            cached = self._cache.get(proxy)
            if cached is not None and time.monotonic() - cached[0] < self._cache_ttl:
                return cached[1]
            result = _measure(proxy)
            download = result.get("download") or {}
            mbps = download.get("best_mbps")
            if not download.get("ok") or not isinstance(mbps, (int, float)) or isinstance(mbps, bool):
                return None
            self._cache[proxy] = (time.monotonic(), float(mbps))
            return float(mbps)
