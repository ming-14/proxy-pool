"""本地链路健康门控：链路可用性判定（每次调用真实探测，无结论缓存）

serve 的心跳/补测/填充都建立在"本地链路可用"这一前提上：链路异常（断网/劣化）
时探测必然失败，若按 per-proxy 记失败，好代理会在数轮内被判为坏代理并删除。
调用方在**探测前**和**探测后**各调一次 check()，两次都正常才允许执行正常拉黑
操作；探测后复检把"探测期间链路劣化"拦在 per-proxy 失败计数之外。

- check()  链路是否可用：并发直连多个探测目标（任一返回状态码 < 400 即正常）。
           每次调用都真实探测（不缓存结论）；并发调用者共享同一次在途探测，
           不重复直连，也不返回探测前的陈旧结论。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

logger = logging.getLogger(__name__)


class LocalNetworkGate:
    """本地链路健康门控（链路可用性判定，每次真实探测）"""

    def __init__(
        self,
        *,
        async_getter,
        probe_urls: Iterable[str],
        probe_timeout: float,
    ) -> None:
        """
        Args:
            async_getter:  async HTTP 请求器（探测直连，走 aiohttp 不经线程池）
            probe_urls:    探测目标（任一返回状态码 < 400 即认为链路可用）
            probe_timeout: 单个探测目标超时秒数
        """
        self._async_getter = async_getter
        self._probe_urls = tuple(probe_urls)
        self._probe_timeout = probe_timeout
        self._inflight: asyncio.Task | None = None  # 在途探测（并发调用者共享）

    async def check(self) -> bool:
        """判定本地链路当前是否可用（每次调用都真实探测）

        并发调用者共享同一次在途探测：既不重复直连，也不返回探测前的陈旧结论。
        """
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._probe())
        # shield：调用方被取消（serve 退出/补测任务取消）不中断共享探测
        return await asyncio.shield(self._inflight)

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
