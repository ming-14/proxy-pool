"""serve 备用队列维护用例：fill（探测补入）+ heartbeat（探活 + 退避补测淘汰）

备用队列（role="standby" 的组）是本地代理服务器的上游集合，本用例维护其成员：

- fill:       队列有缺口时，按配置的填充组优先级收集候选（排除已在队列的代理），
              并发探测；存活代理入队（单一归属），死候选直接拉黑。
- heartbeat:  并发探测队列内代理；成功恢复（清零失败计数），失败计 fails，
              达到所在组阈值（should_blacklist）即拉黑移出队列；
              未达阈值进入退避补测（retry_probe 独立异步任务负责复核，
              成功恢复、失败继续累计直至拉黑）——避免一次瞬时失败就拉黑。
- retry_probe: 对单个待补测代理退避复核：间隔 1s/2s/4s...（上限 30s），
              成功清零恢复；失败累计到阈值拉黑。

fill/heartbeat/retry_probe 均为 async（探测走 CheckProxiesUseCase.execute，
网络请求在 asyncio.to_thread 中执行），serve 事件循环不被同步阻塞。
"""

import asyncio
import logging

from src.domain.proxy_pool import ProxyPool
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.check_proxies import CheckProxiesUseCase

logger = logging.getLogger(__name__)

# 退避补测参数：起始间隔与最大间隔（秒）
RETRY_BACKOFF_INITIAL = 1.0
RETRY_BACKOFF_MAX = 30.0


def _proxy_type(proxy: str) -> str:
    """提取代理类型（scheme 前缀，池内代理均已带 scheme）"""
    return proxy.split("://", 1)[0]


class StandbyQueueUseCase:
    """备用队列维护：fill 补入 + heartbeat 探活淘汰（含退避补测）"""

    def __init__(
        self,
        *,
        pool: ProxyPool,
        record: RecordProxyUseCase,
        check_proxies: CheckProxiesUseCase,
        standby_group: str,
        fill_groups: tuple[str, ...],
        fill_probe_multiplier: int,
        fill_probe_min_extra: int,
    ) -> None:
        """
        Args:
            pool:                   代理池领域模型
            record:                 记录用例（失败计数/拉黑）
            check_proxies:          并发探测用例
            standby_group:          备用队列组名（配置 role="standby"）
            fill_groups:            填充候选组（按优先级顺序，靠前优先）
            fill_probe_multiplier:  候选数 = 缺口 × 倍率
            fill_probe_min_extra:   候选数至少 缺口 + 增量
        """
        self._pool = pool
        self._record = record
        self._check_proxies = check_proxies
        self._standby_group = standby_group
        self._fill_groups = fill_groups
        self._fill_probe_multiplier = fill_probe_multiplier
        self._fill_probe_min_extra = fill_probe_min_extra
        self._retrying: set[str] = set()  # 退避补测中的代理（heartbeat 轮跳过）

    def standby_proxies(self) -> list[str]:
        """备用队列当前代理"""
        return self._pool.get_group_proxies(self._standby_group)

    def pending_retries(self) -> list[str]:
        """当前待退避补测的代理"""
        return list(self._retrying)

    async def fill(self, allowed_types: set[str], probe_url: str, timeout: float,
                   concurrency: int, capacity: int) -> int:
        """探测补入备用队列至容量上限，返回新增数量

        候选按配置的填充组顺序从可用集合收集（排除已在队列的代理，避免
        重复探测与瞬时失败误拉黑队列内代理）：存活入队、死候选拉黑。
        """
        remaining = capacity - len(self.standby_proxies())
        if remaining <= 0:
            return 0

        # 候选数 = 缺口 × 配置倍率（且至少 缺口 + 最小增量），提高存活命中率
        probe_count = max(
            remaining * self._fill_probe_multiplier,
            remaining + self._fill_probe_min_extra,
        )

        available = self._pool.get_available_proxies()
        current_standby = set(self.standby_proxies())
        candidates: list[str] = []
        # 候选上限 probe_count 在收集内循环里即时检查：单个组代理数超过上限时必须截断，
        # 否则一次探测会拉入数千候选（如 freeproxy-world 满池时），耗时且大量误拉黑。
        for group_name in self._fill_groups:
            for p, info in available.items():
                if len(candidates) >= probe_count:
                    break
                if (info["group"] == group_name and p not in candidates
                        and p not in current_standby and _proxy_type(p) in allowed_types):
                    candidates.append(p)
            if len(candidates) >= probe_count:
                break
        if not candidates:
            logger.warning("备用队列补充: 无候选代理")
            return 0

        logger.info("备用队列探测 %d 个候选 -> %s", len(candidates), probe_url)
        results = await self._check_proxies.execute(
            candidates, probe_url, max_concurrent=concurrency, timeout=timeout,
        )

        added = 0
        for proxy in candidates:
            if len(self.standby_proxies()) >= capacity:
                break
            info = results.get(proxy)
            if info is not None and info.get("success"):
                # 移入备用队列（单一归属，remove/晋级不会误删原组残留）
                self._pool.move_proxy(proxy, self._standby_group, fails=0)
                added += 1
                logger.info("备用队列补入 %s", proxy)
            else:
                self._record.remove(proxy)
        logger.info("备用队列补充完成: +%d (队列 %d/%d)", added, len(self.standby_proxies()), capacity)
        return added

    async def heartbeat(self, probe_url: str, timeout: float, concurrency: int) -> list[str]:
        """并发探测队列内代理，返回本轮确认存活的代理

        成功：清零失败计数，保留在队列（不晋级 trusted）；
        失败：失败计数 +1，达到所在组阈值（should_blacklist）即拉黑移出，
              未达阈值进入退避补测（由 retry_probe 复核，heartbeat 轮跳过）。
        """
        proxies = [p for p in self.standby_proxies() if p not in self._retrying]
        if not proxies:
            logger.warning("备用队列无可探测代理，跳过心跳")
            return []

        logger.info("心跳探测 %d 个代理 -> %s", len(proxies), probe_url)
        results = await self._check_proxies.execute(
            proxies, probe_url, max_concurrent=concurrency, timeout=timeout,
        )

        alive: list[str] = []
        for proxy in proxies:
            info = results.get(proxy)
            if info is not None and info.get("success"):
                self._pool.reset_fails(proxy)
                alive.append(proxy)
                logger.info("代理 %s 心跳成功 (状态码 %s, 耗时 %.2fs)",
                            proxy, info.get("status_code"), info.get("elapsed", 0))
            else:
                reason = info.get("error") if info else "无探测结果"
                self._record.record_fail(proxy)
                if self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.warning("代理 %s 心跳失败: %s, 达到阈值已拉黑", proxy, reason)
                else:
                    self._retrying.add(proxy)
                    logger.warning("代理 %s 心跳失败: %s, 进入退避补测", proxy, reason)

        logger.info("心跳完成: %d 存活, %d 待补测 (队列 %d)",
                    len(alive), len(self._retrying), len(self.standby_proxies()))
        return alive

    async def retry_probe(self, proxy: str, probe_url: str, timeout: float) -> None:
        """对单个失败代理退避补测：成功恢复（清 fails），失败累计到阈值拉黑

        独立异步任务（serve 主循环为每个待补测代理启动）；
        结束（成功/拉黑/取消/代理已不在队列）时从待补测集合移除。
        """
        backoff = RETRY_BACKOFF_INITIAL
        try:
            while True:
                if proxy not in self.standby_proxies():
                    logger.info("代理 %s 已不在备用队列, 停止补测", proxy)
                    return
                await asyncio.sleep(backoff)
                results = await self._check_proxies.execute(
                    [proxy], probe_url, max_concurrent=1, timeout=timeout,
                )
                info = results.get(proxy)
                if info is not None and info.get("success"):
                    self._pool.reset_fails(proxy)
                    logger.info("代理 %s 补测成功, 恢复使用", proxy)
                    return
                self._record.record_fail(proxy)
                if self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.warning("代理 %s 补测失败, 达到阈值已拉黑", proxy)
                    return
                backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
                logger.info("代理 %s 补测失败, %.0fs 后重试", proxy, backoff)
        finally:
            self._retrying.discard(proxy)
