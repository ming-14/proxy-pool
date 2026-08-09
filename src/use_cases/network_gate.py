"""本地网络可用性门控：直连多目标判定本地网络是否可用

serve 心跳/补测/fill 前调用 gate.check()，返回 False 时跳过本轮操作——
避免本地网络故障（WiFi 断/ISP 故障/probe_url 不可达）时代理心跳全失败
导致好代理被误拉黑。网络恢复后自动回到正常逻辑。

策略：
- 多目标并发直连，任一通即认为网络正常（容错单目标抽风/被墙）；
- 网络正常时：缓存复用结果（cache_ok 秒内不重复直连），开销几乎为零；
- 网络异常时：退避重检（backoff_initial → ×2 → backoff_max），异常越久探测越稀疏。
"""

import asyncio
import logging
import time
from typing import Iterable

from src.domain.ports import HttpGetter

logger = logging.getLogger(__name__)


class LocalNetworkGate:
    """本地网络可用性门控（直连多目标任一通即认为可用，带缓存与退避）

    serve 各环节（heartbeat/fill/retry_probe）调用 check() 做门控：
    返回 True 正常执行；返回 False 跳过本轮（不计数/不拉黑/不补测），
    队列保持现状，网络恢复后自动恢复调度。
    """

    def __init__(
        self,
        *,
        http_getter: HttpGetter,
        probe_urls: Iterable[str],
        cache_ok: float = 5.0,
        backoff_initial: float = 2.0,
        backoff_max: float = 30.0,
        async_getter=None,
    ) -> None:
        """
        Args:
            http_getter:      HTTP 请求端口（直连，不走代理）
            probe_urls:       探测目标 URL 列表（任一通即认为网络正常）
            cache_ok:         网络正常时缓存秒数（期内复用上次结果，不重复直连）
            backoff_initial:  网络异常时退避重检起始间隔秒
            backoff_max:      退避间隔上限秒
            async_getter:     async HTTP 请求器（aiohttp，避免线程池 C 层泄漏）
        """
        self._http_getter = http_getter
        self._probe_urls = tuple(probe_urls)
        self._cache_ok = cache_ok
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._ok: bool = True                  # 最近一次判定结果
        self._last_check: float = 0.0          # 最近一次直连时刻（monotonic）
        self._backoff: float = backoff_initial  # 当前退避间隔（异常时缓存时长）
        self._async_getter = async_getter

    @property
    def is_ok(self) -> bool:
        """最近一次判定结果（供面板/日志查询，不触发探测）"""
        return self._ok

    async def check(self, timeout: float) -> bool:
        """判定本地网络是否可用（带缓存与退避）

        网络正常时 cache_ok 秒内复用结果；网络异常时按当前退避间隔缓存。
        缓存过期才真正探测（并发直连所有目标，任一通即 True）。
        """
        now = time.monotonic()
        cache_ttl = self._cache_ok if self._ok else self._backoff
        if now - self._last_check < cache_ttl:
            return self._ok

        self._last_check = now
        self._ok = await self._probe(timeout)
        if self._ok:
            self._backoff = self._backoff_initial  # 恢复后退避重置
            logger.debug("本地网络门控: 正常")
        else:
            # 异常时退避倍增（上限 backoff_max），异常越久探测越稀疏
            self._backoff = min(self._backoff * 2, self._backoff_max)
            logger.warning("本地网络门控: 异常（所有探测目标不可达），下次 %.0fs 后重检",
                           self._backoff)
        return self._ok

    async def _probe(self, timeout: float) -> bool:
        """并发直连所有探测目标，任一通即返回 True

        verify=True：探测目标是知名站点（Cloudflare/Google 等），校验证书能通过，
        且能避免被中间人劫持误判为通。直连不走代理，校验目标证书合理。
        """
        async def try_one(url: str) -> bool:
            try:
                await self._async_getter.get(url, timeout=timeout, verify=True)
                return True
            except Exception:
                return False

        results = await asyncio.gather(*(try_one(u) for u in self._probe_urls))
        return any(results)
