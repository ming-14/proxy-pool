"""本地链路健康门控：链路是否可用 + 一轮探测结果是否可信

serve 的心跳/补测/填充都建立在"本地链路可用"这一前提上：链路异常（断网/劣化）
时探测必然失败，若按 per-proxy 记失败，好代理会在数轮内被判为坏代理并删除。
两层判定都收敛在本模块，调用方只投喂事实、不各写一份判断：

- check()       链路是否可用：并发直连多个探测目标（任一返回状态码 < 400 即正常）。
                正常时结论缓存 cache_ok 秒，异常时退避复检（×2 递增到上限）。
- assess_batch() 本轮结果是否可信：失败面过大（失败率 >= 阈值且样本足够）判为链路
                事件，该轮失败不得计入 per-proxy 连续失败；同时作废"链路正常"结论，
                让下次 check() 立即复检，不继续拿劣化链路的结果淘汰代理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

logger = logging.getLogger(__name__)


class LocalNetworkGate:
    """本地链路健康门控（链路可用性判定 + 探测结果可信度判定）"""

    def __init__(
        self,
        *,
        async_getter,
        probe_urls: Iterable[str],
        probe_timeout: float,
        cache_ok: float,
        backoff_initial: float,
        backoff_max: float,
        link_event_fail_ratio: float,
        link_event_min_sample: int,
    ) -> None:
        """
        Args:
            async_getter:          async HTTP 请求器（探测直连，走 aiohttp 不经线程池）
            probe_urls:            探测目标（任一返回状态码 < 400 即认为链路可用）
            probe_timeout:         单个探测目标超时秒数
            cache_ok:              链路正常时的结论缓存秒数（期内复用，不重复直连）
            backoff_initial:       链路异常时的复检起始间隔秒
            backoff_max:           复检间隔上限秒
            link_event_fail_ratio: 判定链路事件的失败率阈值
            link_event_min_sample: 判定链路事件的最小样本数（小批量探测不参与判定）
        """
        self._async_getter = async_getter
        self._probe_urls = tuple(probe_urls)
        self._probe_timeout = probe_timeout
        self._cache_ok = cache_ok
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._link_event_fail_ratio = link_event_fail_ratio
        self._link_event_min_sample = link_event_min_sample
        self._healthy = True                            # 最近一次判定结果
        self._checked_at: float | None = None           # 探测时刻（monotonic）；None = 结论已作废
        self._backoff = backoff_initial                 # 当前复检间隔（异常时也是结论缓存时长）
        self._inflight: asyncio.Task | None = None      # 在途探测（并发调用者共享）

    async def check(self) -> bool:
        """判定本地链路是否可用（正常时结论复用，异常时退避复检）

        并发调用者共享同一次在途探测：既不重复直连，也不返回探测前的陈旧结论。
        """
        now = time.monotonic()
        cache_ttl = self._cache_ok if self._healthy else self._backoff
        if self._checked_at is not None and now - self._checked_at < cache_ttl:
            return self._healthy
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._probe_and_update())
        # shield：调用方被取消（serve 退出/补测任务取消）不中断共享探测
        return await asyncio.shield(self._inflight)

    def assess_batch(self, failed: int, total: int) -> bool:
        """判定一轮探测结果是否可信：True = 按 per-proxy 计分，False = 链路事件不计分

        失败面过大说明问题在链路而不在代理（链路劣化/带宽被挤占/延迟逼近
        超时）。实测：失败候选每轮（心跳 2s）被重复探测，5 次连续失败只需
        10~20 秒即触发拉黑删除（remove_all_copies，黑名单 3 小时），因此链路
        抖动必须拦在这一层，不能落到 per-proxy 失败计数上。

        判为链路事件时作废当前结论，让下次 check() 立即直连复检。
        """
        if failed < self._link_event_min_sample or failed / total < self._link_event_fail_ratio:
            return True
        logger.warning("本地链路疑似劣化: %d/%d 探测失败（>=%.0f%% 且 >=%d 个），"
                       "本轮不计代理失败并立即复检链路",
                       failed, total, self._link_event_fail_ratio * 100,
                       self._link_event_min_sample)
        self._checked_at = None
        return False

    async def _probe_and_update(self) -> bool:
        """直连探测一次并推进状态机（记录探测时刻、退避递增、恢复重置）"""
        try:
            self._checked_at = time.monotonic()  # 先记时刻：同一缓存窗口内只探测一次
            self._healthy = await self._probe()
            if self._healthy:
                self._backoff = self._backoff_initial
                logger.debug("本地链路正常")
            else:
                self._backoff = min(self._backoff * 2, self._backoff_max)
                logger.warning("本地链路异常: %d 个探测目标全部不可达，%.0fs 后复检",
                               len(self._probe_urls), self._backoff)
            return self._healthy
        finally:
            self._inflight = None

    async def _probe(self) -> bool:
        """并发直连所有探测目标，任一返回状态码 < 400 即认为链路可用

        - 校验状态码：透明代理/强制门户/运营商劫持会回 4xx/5xx 错误页，只看
          "有没有响应"会把这类情况误判为链路正常。
        - verify=True：目标是知名站点，校验证书可避免被中间人劫持误判为通。
        """
        async def reachable(url: str) -> bool:
            try:
                resp = await self._async_getter.get(url, timeout=self._probe_timeout, verify=True)
            except Exception:
                return False
            return resp.status_code < 400

        return any(await asyncio.gather(*(reachable(url) for url in self._probe_urls)))
