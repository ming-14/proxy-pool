"""serve 代理服务队列维护用例：fill（流式探测补入 + 响应时间排序淘汰）+ heartbeat（探活 + 退避补测）

代理服务队列（role="standby" 的组）是本地代理服务器的上游集合，本用例维护其成员：

- fill:       队列有缺口时，按配置的填充组优先级收集候选（排除已在队列的代理），
              并发探测；**流式入队**——每个代理探测完成即处理（不等全部完成）：
              存活按 latency 插入队列（队列满时与队尾比较，更快则踢尾入新、较慢则
              move trusted），失败 record_fail 累计到 max_consecutive_fails 才拉黑。
              队列始终按 latency 升序维护（最快的在上游 rc 调度时更易命中）。
- heartbeat:  并发探测队列内代理；成功更新 latency + 清零连续失败计数，失败计 consecutive_fails，
              达到所在组阈值（should_blacklist）即拉黑移出；
              未达阈值进入退避补测（retry_probe 独立异步任务负责复核，
              成功恢复、失败继续累计直至拉黑）——避免一次瞬时失败就拉黑。
- retry_probe: 对单个待补测代理退避复核：间隔 1s/2s/4s...（上限 30s），
              成功更新 latency + 清零恢复；失败累计到阈值拉黑。

**本地网络门控**：fill/heartbeat/retry_probe 开头调 network_gate.check()，
本地网络异常（WiFi 断/ISP 故障/探测目标不可达）时跳过本轮操作（不计数/不拉黑/
不补测），队列保持现状，网络恢复后自动恢复调度——避免好代理被误拉黑。

fill/heartbeat/retry_probe 均为 async（探测走 CheckProxiesUseCase.execute，
网络请求在 asyncio.to_thread 中执行），serve 事件循环不被同步阻塞。
"""

import asyncio
import logging
import time
from collections import deque
from typing import Callable, NamedTuple

from src.domain.proxy_pool import ProxyPool, LOCKED_GROUP
from src.domain.models import bind_of
from src.use_cases.network_gate import LocalNetworkGate
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.check_proxies import CheckProxiesUseCase

logger = logging.getLogger(__name__)

# 退避补测参数：起始间隔与最大间隔（秒）
RETRY_BACKOFF_INITIAL = 1.0
RETRY_BACKOFF_MAX = 30.0


class StandbySample(NamedTuple):
    """代理服务队列单次采样指标（NamedTuple 紧凑存储，~80 字节/点 vs dict ~232 字节）

    每轮 heartbeat 结束采一次，按 metrics_window 时间窗口保留；前端图表据此重绘趋势。
    locked=True 标记锁定期间采集：延迟图正常显示测速数据，健康率图该点置 null 断开
    （锁定时健康率/队列长度无意义）。
    """
    ts: float                  # 采样真实时间戳（time.time()，前端 x 轴 HH:MM:SS）
    avg: float | None          # 平均延迟（秒），无可用代理时 None
    min: float | None          # 最小延迟
    max: float | None          # 最大延迟
    health_rate: int           # 健康率 0-100（consecutive_fails==0 占比）
    queue_length: int          # 队列长度
    locked: bool = False       # 是否锁定期间采集（健康率图该点断开，延迟图照常显示）


class ProxyLatencySample(NamedTuple):
    """单代理延时采样点（per-proxy latency history）

    heartbeat/retry_probe 成功时记一笔，按 metrics_window 时间窗口保留。
    key 为 bind_of(proxy)（地址，不含 scheme），与 per-proxy 流量历史统一 key。
    """
    ts: float                  # 采样时间戳
    latency: float             # 探测响应时间（秒）


def _proxy_type(proxy: str) -> str:
    """提取代理类型（scheme 前缀，池内代理均已带 scheme）"""
    return proxy.split("://", 1)[0]


class StandbyQueueUseCase:
    """代理服务队列维护：fill 流式补入 + 响应时间排序淘汰 + heartbeat 探活（含退避补测）"""

    def __init__(
        self,
        *,
        pool: ProxyPool,
        record: RecordProxyUseCase,
        check_proxies: CheckProxiesUseCase,
        network_gate: LocalNetworkGate,
        standby_group: str,
        fill_groups: tuple[str, ...],
        fill_probe_multiplier: int,
        fill_probe_min_extra: int,
        metrics_window: float,
    ) -> None:
        """
        Args:
            pool:                   代理池领域模型
            record:                 记录用例（连续失败计数/拉黑）
            check_proxies:          并发探测用例
            network_gate:           本地网络门控（异常时跳过 fill/heartbeat/补测，避免误拉黑）
            standby_group:          代理服务队列组名（配置 role="standby"）
            fill_groups:            填充候选组（按优先级顺序，靠前优先）
            fill_probe_multiplier:  候选数 = 缺口 × 倍率
            fill_probe_min_extra:   候选数至少 缺口 + 增量
            metrics_window:         指标采样存储时长秒（窗口）。每轮心跳采一次，
                                    超出窗口的自动清理；心跳动态调整时窗口始终不变
        """
        self._pool = pool
        self._record = record
        self._check_proxies = check_proxies
        self._gate = network_gate
        self._standby_group = standby_group
        self._fill_groups = fill_groups
        self._fill_probe_multiplier = fill_probe_multiplier
        self._fill_probe_min_extra = fill_probe_min_extra
        self._retrying: set[str] = set()  # 退避补测中的代理（heartbeat 轮跳过）
        self._capacity: int | None = None  # 队列容量（serve 启动时 set，standby_proxies 截断超出容量的固定代理）
        self._metrics_window = metrics_window
        self._history: deque[StandbySample] = deque()  # 采样历史，按时间窗口清理过期点
        # 按代理地址（bind）的延时采样历史，供 /api/proxy/history 按需查询
        self._per_proxy_latency_history: dict[str, deque[ProxyLatencySample]] = {}

    def set_capacity(self, capacity: int) -> None:
        """设置队列容量（serve 启动时调用，用于截断超出容量的固定代理）"""
        self._capacity = capacity

    @property
    def standby_group(self) -> str:
        """代理服务队列组名（供 API 层校验 pin/unpin 目标）"""
        return self._standby_group

    def update_fill_probe_params(self, fill_probe_multiplier: int, fill_probe_min_extra: int) -> None:
        """运行时更新填充候选数参数（Web 面板调整调度策略时调用，serve 下次 fill 生效）"""
        self._fill_probe_multiplier = fill_probe_multiplier
        self._fill_probe_min_extra = fill_probe_min_extra

    def history(self) -> list[StandbySample]:
        """采样历史（按时间正序），供 /api/status 下发前端图表重绘"""
        return list(self._history)

    def per_proxy_latency_history(self, bind: str) -> list[ProxyLatencySample]:
        """指定代理地址的延时采样历史（按时间正序），供 /api/proxy/history 按需查询

        key 为 bind_of(proxy)（地址，不含 scheme），与 per-proxy 流量历史统一 key。
        仅内存，重启清零。
        """
        return list(self._per_proxy_latency_history.get(bind, ()))

    def _append_per_proxy_latency(self, proxy: str, latency: float) -> None:
        """记录单代理延时采样点并按时间窗口清理过期点（heartbeat/retry_probe 成功时调）"""
        bind = bind_of(proxy)
        dq = self._per_proxy_latency_history.get(bind)
        if dq is None:
            dq = deque()
            self._per_proxy_latency_history[bind] = dq
        sample = ProxyLatencySample(time.time(), latency)
        dq.append(sample)
        cutoff = sample.ts - self._metrics_window
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def _sample(self, proxies: list[str]) -> None:
        """采样本轮心跳探测对象的指标并按时间窗口清理过期点

        每轮 heartbeat 结束调一次，传入本轮探测的代理集合（锁定时为锁定代理，
        否则为代理服务队列成员）。按 metrics_window 时间窗口清理过期点：
        心跳动态调整时采样频率跟着变，窗口始终 = metrics_window。
        """
        latencies = [l for l in (self._pool.get_latency(p) for p in proxies) if l is not None]
        avg = sum(latencies) / len(latencies) if latencies else None
        mn = min(latencies) if latencies else None
        mx = max(latencies) if latencies else None
        total = len(proxies)
        healthy = sum(1 for p in proxies if (self._pool.get_consecutive_fail_count(p) or 0) == 0)
        rate = round(healthy / total * 100) if total else 0
        self._append_sample(StandbySample(time.time(), avg, mn, mx, rate, total, self.is_locked()))

    def _append_sample(self, sample: StandbySample) -> None:
        """追加一个采样点并按时间窗口清理过期点（_sample 与锁定断点共用）"""
        self._history.append(sample)
        cutoff = sample.ts - self._metrics_window
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()

    def standby_proxies(self) -> list[str]:
        """代理服务队列当前代理（完整队列，用于展示/指标/容量判断/心跳探测）

        固定代理优先（按固定时间 pinned_at 升序），非固定代理按 latency 升序排后。
        超出 _capacity 的固定代理忽略（保留在组内不移出，但不参与队列/上游/心跳），
        用户解固其他代理使固定数 <= capacity 后自动重新生效。

        注意：本方法始终返回完整队列，不因锁定状态改变。pproxy 上游同步请用
        upstream_proxies()（锁定时只返回锁定代理）。
        """
        proxies = self._pool.get_group_proxies(self._standby_group)
        pinned = sorted(
            [p for p in proxies if self._pool.is_pinned(p)],
            key=lambda p: self._pool.get_pinned_at(p) or 0,
        )
        others = sorted(
            [p for p in proxies if not self._pool.is_pinned(p)],
            key=lambda p: float("inf") if (l := self._pool.get_latency(p)) is None else l,
        )
        result = pinned + others
        if self._capacity is not None:
            result = result[:self._capacity]
        return result

    def upstream_proxies(self) -> list[str]:
        """pproxy 上游同步列表：锁定时只返回锁定代理，否则返回完整队列

        锁定状态（LOCKED_GROUP 有代理）时 pproxy 唯一上游为锁定代理，
        无论心跳是否成功；否则上游 = 代理服务队列完整成员（standby_proxies）。
        """
        locked = self._pool.get_group_proxies(LOCKED_GROUP)
        if locked:
            return list(locked)
        return self.standby_proxies()

    def pending_retries(self) -> list[str]:
        """当前待退避补测的代理"""
        return list(self._retrying)

    def is_locked(self) -> bool:
        """是否已锁定单一上游（LOCKED_GROUP 有代理）"""
        return bool(self._pool.get_group_proxies(LOCKED_GROUP))

    def lock(self, proxy: str) -> None:
        """锁定代理为 pproxy 唯一上游：move 到 LOCKED_GROUP

        锁定后 upstream_proxies 只返回该代理，fill 跳过（不补入）；心跳仍探测
        锁定代理测速（数据加趋势图），但失败只计数不拉黑不移出——无论心跳是否
        成功都只用该代理。代理须在代理服务队列组内。
        """
        self._pool.move_proxy(proxy, LOCKED_GROUP, consecutive_fails=0)
        logger.info("代理 %s 已锁定为唯一上游", proxy)

    def unlock(self) -> str | None:
        """解除锁定：LOCKED_GROUP 代理 move 回代理服务队列，返回解锁的代理（无则 None）"""
        unlocked = None
        for p in self._pool.get_group_proxies(LOCKED_GROUP):
            self._pool.move_proxy(p, self._standby_group, consecutive_fails=0)
            unlocked = p
        if unlocked:
            logger.info("代理 %s 解除锁定，回到代理服务队列", unlocked)
        return unlocked

    async def fill(self, allowed_types: set[str], probe_url: str, timeout: float,
                   concurrency: int, capacity: int,
                   *, on_change: Callable[[], None] | None = None) -> int:
        """流式探测补入代理服务队列，返回新增数量

        候选按配置的填充组顺序从可用集合收集（排除已在队列的代理，避免重复探测）。
        每个代理探测完成即处理（on_result 回调，不等全部完成）：
          - 存活 + 队列未满：move 入 standby（记 latency），调 on_change（实时 sync 上游）
          - 存活 + 队列满 + latency < 队尾：踢队尾 move trusted，新代理入 standby，调 on_change
          - 存活 + 队列满 + latency >= 队尾：新代理 move trusted（响应太快没进队列不浪费探测成果）
          - 失败：record_fail，should_blacklist 才 remove 拉黑，否则留原组（下次 fill 仍可被选）

        Args:
            on_change: 每次队列成员变更（入队/踢尾）后调（serve 传 lambda 实时同步 pproxy 上游）
        """
        # 本地网络门控：异常时跳过填充（fill 探测会全失败，污染候选代理失败计数）
        if not await self._gate.check(timeout):
            logger.warning("本地网络异常，跳过本轮代理服务队列填充")
            return 0

        # 锁定状态：只用单一上游，无需补充
        if self.is_locked():
            logger.info("已锁定单一上游，跳过填充")
            return 0

        remaining = capacity - len(self.standby_proxies())
        if remaining <= 0:
            return 0

        # 候选数 = 缺口 × 配置倍率（且至少 缺口 + 最小增量），提高存活命中率
        probe_count = max(
            remaining * self._fill_probe_multiplier,
            remaining + self._fill_probe_min_extra,
        )

        available = self._pool.get_available_proxies()
        # 全组代理（含被 capacity 截断忽略的固定代理），避免忽略的固定代理被当候选重新探测
        current_standby = set(self._pool.get_group_proxies(self._standby_group))
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
            logger.warning("代理服务队列补充: 无候选代理")
            return 0

        logger.info("代理服务队列探测 %d 个候选 -> %s", len(candidates), probe_url)
        added = 0

        def on_result(proxy: str, result: dict) -> None:
            """流式入队回调：探测完成即处理，不等全部完成"""
            nonlocal added
            latency = result.get("elapsed")
            if result.get("success"):
                current = self.standby_proxies()  # 已按 latency 升序
                if len(current) < capacity:
                    # 队列未满：入队
                    self._pool.move_proxy(proxy, self._standby_group, consecutive_fails=0, latency=latency)
                    added += 1
                    if on_change:
                        on_change()
                    logger.info("代理服务队列补入 %s (latency=%.2fs)", proxy, latency or 0)
                else:
                    # 队列满：与非固定队尾（最慢）比较，更快则替换
                    tail = current[-1]
                    if self._pool.is_pinned(tail):
                        # 队尾是固定代理（队列全固定），不能踢，新代理 move trusted
                        self._record.record_success(proxy)
                        logger.info("代理 %s 探测存活但队列全固定 (%.2fs), move trusted", proxy, latency or 0)
                    else:
                        tail_latency = self._pool.get_latency(tail)
                        if tail_latency is None or latency < tail_latency:
                            # 踢队尾：直接 move 到 trusted（不用 record_success——它对 standby 成员是
                            # 复制不删除，会残留队列导致超容）
                            self._pool.move_proxy(tail, self._pool.trusted_group, consecutive_fails=0)
                            self._pool.move_proxy(proxy, self._standby_group, consecutive_fails=0, latency=latency)
                            added += 1
                            if on_change:
                                on_change()
                            logger.info("代理服务队列替换: 踢出 %s (%.2fs), 补入 %s (%.2fs)",
                                        tail, tail_latency or 0, proxy, latency or 0)
                        else:
                            # 新代理较慢，move trusted（探测成果不浪费，下次 fill 优先入队）
                            self._record.record_success(proxy)
                            logger.info("代理 %s 探测存活但较慢 (%.2fs >= %.2fs), move trusted",
                                        proxy, latency or 0, tail_latency or 0)
            else:
                # 失败：record_fail 累计，达到 max_consecutive_fails 才拉黑
                self._record.record_fail(proxy)
                if self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.warning("候选 %s 探测失败, 达到阈值已拉黑", proxy)
                else:
                    logger.info("候选 %s 探测失败, consecutive_fails=%d", proxy, self._record.get_consecutive_fail_count(proxy))

        await self._check_proxies.execute(
            candidates, probe_url, max_concurrent=concurrency, timeout=timeout, on_result=on_result,
        )
        logger.info("代理服务队列补充完成: +%d (队列 %d/%d)", added, len(self.standby_proxies()), capacity)
        return added

    async def heartbeat(self, probe_url: str, timeout: float, concurrency: int) -> list[str]:
        """并发探测队列内代理，返回本轮确认存活的代理

        成功：更新 latency + 清零连续失败计数，保留在队列（不晋级 trusted）；
        失败：连续失败计数 +1，达到所在组阈值（should_blacklist）即拉黑移出，
              未达阈值进入退避补测（由 retry_probe 复核，heartbeat 轮跳过）。

        锁定时：仍探测锁定代理测速（数据加趋势图），但失败只计数不拉黑不移出
        不补测——无论心跳是否成功都只用该代理（锁定语义不变）。
        """
        # 本地网络门控：异常时跳过本轮心跳（不计数/不拉黑/不补测），队列保持现状
        if not await self._gate.check(timeout):
            logger.warning("本地网络异常，跳过本轮心跳（不计数/不拉黑）")
            return []

        locked = self.is_locked()
        if locked:
            # 锁定时探测锁定代理测速（upstream_proxies 只返回锁定代理）
            proxies = self.upstream_proxies()
        else:
            proxies = [p for p in self.standby_proxies() if p not in self._retrying]
        if not proxies:
            logger.warning("代理服务队列无可探测代理，跳过心跳")
            return []

        logger.info("心跳探测 %d 个代理 -> %s", len(proxies), probe_url)
        results = await self._check_proxies.execute(
            proxies, probe_url, max_concurrent=concurrency, timeout=timeout,
        )

        alive: list[str] = []
        for proxy in proxies:
            info = results.get(proxy)
            if info is not None and info.get("success"):
                self._pool.reset_consecutive_fails(proxy)
                self._pool.update_latency(proxy, info.get("elapsed", 0))
                self._append_per_proxy_latency(proxy, info.get("elapsed", 0))
                alive.append(proxy)
                logger.info("代理 %s 心跳成功 (状态码 %s, 耗时 %.2fs)",
                            proxy, info.get("status_code"), info.get("elapsed", 0))
            else:
                reason = info.get("error") if info else "无探测结果"
                self._record.record_fail(proxy)
                if locked or self._pool.is_pinned(proxy):
                    # 锁定/固定代理：只计数，不补测、不拉黑、不移出，下轮心跳再探测
                    logger.warning("代理 %s 心跳失败: %s, 锁定/固定代理仅计数 (consecutive_fails=%d)",
                                   proxy, reason, self._record.get_consecutive_fail_count(proxy))
                elif self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.warning("代理 %s 心跳失败: %s, 达到阈值已拉黑", proxy, reason)
                else:
                    self._retrying.add(proxy)
                    logger.warning("代理 %s 心跳失败: %s, 进入退避补测", proxy, reason)

        logger.info("心跳完成: %d 存活, %d 待补测 (队列 %d)",
                    len(alive), len(self._retrying), len(self.standby_proxies()))
        self._sample(proxies)
        return alive

    async def retry_probe(self, proxy: str, probe_url: str, timeout: float) -> None:
        """对单个失败代理退避补测：成功恢复（更新 latency + 清零 consecutive_fails），失败累计到阈值拉黑

        独立异步任务（serve 主循环为每个待补测代理启动）；
        结束（成功/拉黑/取消/代理已不在队列）时从待补测集合移除。
        """
        backoff = RETRY_BACKOFF_INITIAL
        try:
            while True:
                if proxy not in self.standby_proxies():
                    logger.info("代理 %s 已不在代理服务队列, 停止补测", proxy)
                    return
                await asyncio.sleep(backoff)
                # 本地网络门控：异常时不计数/不拉黑/不增加退避（非代理问题），
                # 重置退避到初始值快速轮询 gate 缓存，网络恢复后立即恢复补测
                if not await self._gate.check(timeout):
                    backoff = RETRY_BACKOFF_INITIAL
                    logger.info("代理 %s 补测因本地网络异常暂停，等待网络恢复", proxy)
                    continue
                results = await self._check_proxies.execute(
                    [proxy], probe_url, max_concurrent=1, timeout=timeout,
                )
                info = results.get(proxy)
                if info is not None and info.get("success"):
                    self._pool.reset_consecutive_fails(proxy)
                    self._pool.update_latency(proxy, info.get("elapsed", 0))
                    self._append_per_proxy_latency(proxy, info.get("elapsed", 0))
                    logger.info("代理 %s 补测成功, 恢复使用", proxy)
                    return
                self._record.record_fail(proxy)
                if self._pool.is_pinned(proxy):
                    # 固定代理不拉黑（防御：固定代理不进 _retrying，正常不会被补测）
                    logger.warning("代理 %s 补测失败, 固定代理不拉黑 (consecutive_fails=%d)",
                                   proxy, self._record.get_consecutive_fail_count(proxy))
                    return
                if self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.warning("代理 %s 补测失败, 达到阈值已拉黑", proxy)
                    return
                backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
                logger.info("代理 %s 补测失败, %.0fs 后重试", proxy, backoff)
        finally:
            self._retrying.discard(proxy)
