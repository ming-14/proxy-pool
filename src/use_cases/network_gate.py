"""本地链路健康门控：链路可用性判定

serve 的心跳/补测/填充都建立在"本地链路可用"这一前提上：链路异常（断网/劣化）
时探测必然失败，若按 per-proxy 记失败，好代理会在数轮内被判为坏代理并删除。
调用方在**探测前**和**探测后**各调一次 check()，两次都正常才允许执行正常拉黑
操作；探测后复检把"探测期间链路劣化"拦在 per-proxy 失败计数之外。

结论缓存是单向的：**只缓存"不可用"，"可用"永不缓存**。两种结论的后果不对称——
"不可用"让调用方少做一轮（跳过心跳/填充、补测暂停），复用只花时间不花正确性；
"可用"授权调用方按 per-proxy 计分并拉黑，一旦陈旧（链路已劣化而缓存仍判正常），
失败就会归因到代理被误删（心跳 2s 一轮，攒满拉黑阈值只要 10~20 秒）。

- check()  链路是否可用：并发直连多个探测目标（任一返回状态码 < 400 即正常）。
           失败结论在 unhealthy_cache_ttl 内直接复用、不再探测；成功结论每次真实
           探测。并发调用者共享同一次在途探测，不重复直连。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

logger = logging.getLogger(__name__)


class LocalNetworkGate:
    """本地链路健康门控（失败结论可缓存，成功结论每次真实探测）"""

    def __init__(
        self,
        *,
        async_getter,
        probe_urls: Iterable[str],
        probe_timeout: float,
        unhealthy_cache_ttl: float,
    ) -> None:
        """
        Args:
            async_getter:        async HTTP 请求器（探测直连，走 aiohttp 不经线程池）
            probe_urls:          探测目标（任一返回状态码 < 400 即认为链路可用）
            probe_timeout:       单个探测目标超时秒数
            unhealthy_cache_ttl: "链路不可用"结论的复用时长秒（0 = 不缓存）。
                                 缓存期内不发起探测（调用方轮询不再产生外网请求），
                                 因此它同时是链路恢复的最大感知延迟。
        """
        self._async_getter = async_getter
        self._probe_urls = tuple(probe_urls)
        self._probe_timeout = probe_timeout
        self._unhealthy_cache_ttl = unhealthy_cache_ttl
        self._inflight: asyncio.Task | None = None  # 在途探测（并发调用者共享）
        self._unhealthy_until = 0.0                 # 失败结论缓存截止时刻（0 = 无缓存）

    async def check(self) -> bool:
        """判定本地链路当前是否可用

        失败结论在缓存期内复用（调用方高频轮询不再放大对外探测），成功结论每次
        真实探测。并发调用者共享同一次在途探测，既不重复直连，也不返回探测前的
        陈旧成功结论（陈旧成功会授权 per-proxy 拉黑）。
        """
        if time.monotonic() < self._unhealthy_until:
            return False
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._probe())
        # shield：调用方被取消（serve 退出/补测任务取消）不中断共享探测
        healthy = await asyncio.shield(self._inflight)
        if healthy:
            if self._unhealthy_until:
                logger.info("本地链路恢复可用")
                self._unhealthy_until = 0.0
            return True
        self._unhealthy_until = time.monotonic() + self._unhealthy_cache_ttl
        logger.warning("本地链路不可用（失败结论缓存 %.0fs，期间不再探测）",
                       self._unhealthy_cache_ttl)
        return False

    async def _probe(self) -> bool:
        """并发直连所有探测目标，任一返回状态码 < 400 即认为链路可用

        - 校验状态码：透明代理/强制门户/运营商劫持会回 4xx/5xx 错误页，只看
          "有没有响应"会把这类情况误判为链路正常。
        - verify=True：目标是知名站点，校验证书可避免被中间人劫持误判为通。
        """
        try:
            async def reachable(url: str) -> bool:
                try:
                    resp = await self._async_getter.get(url, timeout=self._probe_timeout, verify=True)
                except Exception:
                    return False
                return resp.status_code < 400

            return any(await asyncio.gather(*(reachable(url) for url in self._probe_urls)))
        finally:
            self._inflight = None
