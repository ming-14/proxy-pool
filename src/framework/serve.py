"""serve 命令主循环：本地代理服务器 + 代理服务队列维护

代理服务队列组（配置 role="standby" 的组）作为 pproxy 上游（有界，容量见
STANDBY_QUEUE_CAPACITY）。队列维护业务（探测补入/心跳淘汰/退避补测）
在 StandbyQueueUseCase（use_cases 层），本模块只负责：
    调度：启动填充 → 周期心跳 → 为待补测代理启动独立复核任务 → 同步上游；
    采样：流量采样独立循环（固定间隔，不随心跳放宽——锁定时心跳降频但流量照常采）；
    生命周期：启动/停止事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Callable

from src.domain.models import bind_of
from src.framework import config
from src.framework.loop import run_coro
from src.framework.web.control import EnqueueRuleSettings, ServeControl
from src.framework.web.panel_state import PanelTasks
from src.interfaces.doh_resolver import DohResolver
from src.interfaces.proxy_server import PproxyServer, listen_uri
from src.use_cases.dns_probe import DnsProbeQueue
from src.use_cases.traffic_stats import TrafficStatsUseCase

logger = logging.getLogger(__name__)

# 流量采样间隔秒：独立于心跳节奏。流量采样只读内存计数器（开销极小），
# 锁定单一上游时心跳放宽到 heartbeat_lock_interval 但采样保持此频率——否则
# 正被使用的锁定代理流量图会跟着变稀疏（见 traffic_sample_loop）
TRAFFIC_SAMPLE_INTERVAL = 2.0


def sync_retry_tasks(
    retry_tasks: dict[str, asyncio.Task],
    pending: list[str],
    start: Callable[[str], asyncio.Task],
) -> None:
    """把补测任务表与待补测集合对齐：先回收已结束的任务，再为新增的待补测代理起任务

    两步顺序不可颠倒：已结束但尚未回收的任务会占住 key，使同一代理本轮的新补测
    不被启动，白等一轮到下次心跳才补上。
    """
    for p in [p for p, t in retry_tasks.items() if t.done()]:
        del retry_tasks[p]
    for p in pending:
        if p not in retry_tasks:
            retry_tasks[p] = start(p)


def _proxy_lookup(standby) -> Callable[[str], str | None]:
    """bind → 当前服务队列代理 URL 的查询闭包（口径 = StandbyQueueUseCase.queue_rows）

    DNS 探测队列判定用：bind 不在当前队列（已被淘汰/移出）即跳过探测。
    每次调用重建视图：队列在探测周期之间可能变化，缓存视图会让新入队代理漏探测。
    """
    def lookup(bind: str) -> str | None:
        return next((p for p in standby.queue_rows() if bind_of(p) == bind), None)
    return lookup


async def _run(
    c,
    *,
    interval: int,
    probe_url: str,
    allowed_types: set[str],
    concurrency: int,
    host: str,
    port: int,
    capacity: int,
    web_host: str | None = None,
    web_port: int | None = None,
    web_enabled: bool = True,
) -> None:
    """服务器主循环：启动 + 周期心跳 + 队列补充 + 退避补测 + Web 控制面板（web_enabled=false 时跳过面板）"""
    # DNS 探测队列（流量统计的子模块）：有数据入队即刻穿对应代理探测；geo 缺失时只产 ip 行。
    # DoH 解析器随队列在 serve 内构造：它是 serve 专属依赖，不进容器（所有 CLI 命令都会组
    # 装容器，把只有 serve 用的适配器挂在容器上只会增加面）。
    dns_probe = None
    if config.DNS_PROBE_ENABLED:
        dns_probe = DnsProbeQueue(
            resolver=DohResolver(config.DNS_PROBE_DOH_ENDPOINTS, c.async_getter,
                                 timeout=config.DNS_PROBE_TIMEOUT),
            proxy_lookup=_proxy_lookup(c.standby),
            geo=c.geo,
            concurrency=config.DNS_PROBE_CONCURRENCY,
            cache_ttl=config.DNS_PROBE_CACHE_TTL,
        )
    # 流量统计用例：统计状态与口径全在此，PproxyServer 只转发 modstat 事件。
    # store 同时充当历史条目存储与 DNS 探测结果存储（两条端口都由 SqliteProxyStore 实现）。
    traffic = TrafficStatsUseCase(
        store=c.store,
        dns_probe_store=c.store,
        dns_probe=dns_probe,
        metrics_window=config.METRICS_WINDOW,
        flush_interval=config.TRAFFIC_FLUSH_INTERVAL,
        retention=config.TRAFFIC_RETENTION,
        dns_probe_prune_after=config.DNS_PROBE_PRUNE_AFTER,
    )
    if dns_probe is not None:
        # 结果交回流量统计落盘（后置注入：破除"统计持有队列、队列回调统计"的构造期循环依赖）
        dns_probe.set_sink(traffic.note_probe_result)
        logger.info("DNS 探测已启用: DoH 端点 %d 个, 并发 %d",
                    len(config.DNS_PROBE_DOH_ENDPOINTS), config.DNS_PROBE_CONCURRENCY)
    server = PproxyServer(listen_uri=listen_uri(host, port, config.SERVER_LISTEN_PROTOCOLS),
                          traffic=traffic)
    standby = c.standby
    standby.set_capacity(capacity)  # 队列容量：standby_proxies 按容量截断进上游的候选（超出者仍受心跳探测）
    # 调度参数集中持有：serve 各环节每轮读取，Web 面板可在运行时调整（写同一实例）
    control = ServeControl(
        interval=interval,
        concurrency=concurrency,
        timeout=config.HEARTBEAT_TIMEOUT,
        probe_url=probe_url,
        allowed_types=allowed_types,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
        salgorithm="rc",
    )
    # 入队筛选规则的运行时设置（启停/阈值）：与 control 同层，面板写、fill 读
    enqueue_settings = EnqueueRuleSettings(config.ENQUEUE_RULES)

    # save 节流：每 30 秒持久化一次（c.save 序列化全池 + 文件 I/O，每 2 秒一轮太频繁）
    _last_save = 0.0
    _SAVE_INTERVAL = 30.0

    def sync_upstream() -> None:
        """同步 pproxy 上游列表与存活标记（轻量，每轮调用）

        unhealthy 取正在退避补测的队列成员：它们在 pproxy 侧被标记 alive=False，
        rc/rr/lc 调度不再选中——否则心跳判失败的代理在补测成功/拉黑之前仍会被随机
        选中，把请求交给已知死掉的代理（pproxy 自带的存活检查只在 CLI 里启动，
        本项目不用；存活判定以代理池心跳为唯一来源）。
        """
        server.sync(standby.upstream_proxies(), unhealthy=standby.pending_retries())

    def sync_and_save() -> None:
        """同步 pproxy 上游 + 节流持久化（30 秒一次，非每轮）"""
        sync_upstream()
        nonlocal _last_save
        now = time.monotonic()
        if now - _last_save >= _SAVE_INTERVAL:
            _last_save = now
            c.save()

    def sync_and_save_now() -> None:
        """同步 pproxy 上游 + 立即持久化（lock/unlock/补测完成等变更时调用）"""
        sync_upstream()
        c.save()

    async def fill_if_needed() -> None:
        """缺口补入并同步上游（fill 期间实时 sync，结束后统一 sync + save）

        on_change 回调让 fill 流式入队时每个代理入队即同步 pproxy 上游（含存活标记），
        探测成功的代理立即可用，不必等整批探测完成。
        """
        if len(standby.standby_members()) < capacity:
            await standby.fill(control.allowed_types, control.probe_url, control.timeout,
                               control.concurrency, capacity, on_change=sync_upstream)
        sync_and_save()

    # 启动：探测补入代理服务队列并启动服务器
    await standby.fill(control.allowed_types, control.probe_url, control.timeout,
                       control.concurrency, capacity)
    await server.start(standby.upstream_proxies())
    c.save()
    if standby.upstream_proxies():
        logger.info("服务器就绪: 上游 %d 个代理", len(standby.upstream_proxies()))
    else:
        logger.warning("警告: 代理服务队列仍无可用 %s 类型代理", ",".join(sorted(control.allowed_types)))

    # Web 控制面板（挂载到本事件循环，共享 control 实现运行时调度调整）
    # web.enabled=false 时不构造面板（延迟导入，不加载 aiohttp，省内存）
    panel = None
    if web_enabled:
        from src.framework.web.api import PanelContext
        from src.framework.web.server import WebPanel
        panel = WebPanel(PanelContext(
            pool=c.pool,
            fetch=c.fetch,
            record=c.record,
            standby=standby,
            control=control,
            enqueue_settings=enqueue_settings,
            server=server,
            traffic=traffic,
            save=c.save,
            capacity=capacity,
            purity=c.purity,
            geo=c.geo,
            speedtest=c.speedtest,
            tasks=PanelTasks(),
        ))
        if web_port:
            await panel.start(web_host or "127.0.0.1", web_port)

    # 退避补测任务表：proxy -> asyncio.Task（失败代理独立复核，完成即清出）
    retry_tasks: dict[str, asyncio.Task] = {}

    async def retry_with_sync(p: str) -> None:
        """补测完成后同步上游 + 立即持久化（成功恢复/达到阈值拉黑都会改变队列成员）"""
        try:
            await standby.retry_probe(p, control.probe_url, control.timeout)
        finally:
            sync_and_save_now()

    def start_retry(p: str) -> asyncio.Task:
        """启动单个待补测代理的复核任务（任务表由 sync_retry_tasks 维护）"""
        return asyncio.create_task(retry_with_sync(p))

    async def heartbeat_loop():
        """周期心跳：轮次绝不重叠，间隔至少 interval

        探测（fill/heartbeat）为 async（内部 to_thread 投递网络请求），
        事件循环保持响应 pproxy 请求；下一轮最早从「本轮开始时刻 + interval」
        起算（且不小于当前时刻），即两轮间隔 >= interval，轮内耗时超过
        interval 时立即开始下一轮，避免连续探测刷屏，也不累积 sleep 计时误差。

        锁定单一上游时心跳间隔放宽（锁定代理不淘汰不拉黑，仅测速展示用，
        保持低频率探测即可），避免对锁定代理每 2 秒打一次外网探测。
        """
        next_round = time.monotonic()
        while True:
            now = time.monotonic()
            if next_round > now:
                await asyncio.sleep(next_round - now)
            round_start = time.monotonic()
            try:
                # 轮次前后各补一次缺口（心跳可能淘汰队列成员腾出缺口）
                await fill_if_needed()
                # 心跳：无探测对象时内部直接返回（仍会回收延时历史 key）
                await standby.heartbeat(control.probe_url, control.timeout, control.concurrency)
                await fill_if_needed()
                # 为失败后进入退避补测的代理启动独立复核任务（不占心跳轮次）
                sync_retry_tasks(retry_tasks, standby.pending_retries(), start_retry)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("心跳循环异常: %s", e)
            # 下一轮最早从本轮开始时刻 + interval 起算（间隔至少 interval，绝不重叠）
            # round_interval 为局部值：锁定放宽只影响本循环的轮次间隔，不遮蔽外层 interval 参数
            round_interval = max(control.interval, config.HEARTBEAT_LOCK_INTERVAL if standby.is_locked() else 0)
            next_round = max(time.monotonic(), round_start + round_interval)

    async def traffic_sample_loop():
        """周期流量采样：固定间隔（TRAFFIC_SAMPLE_INTERVAL），不随心跳节奏放宽

        流量采样只读内存计数器（traffic.sample 差分 + 裁剪 deque），与探测解耦——
        锁定单一上游时心跳放宽到 heartbeat_lock_interval（30s），若采样挂在心跳轮次上，
        正被使用的锁定代理流量图会跟着变稀疏；探测阻塞/退避期间采样也不中断。
        """
        while True:
            try:
                traffic.sample(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("流量采样异常: %s", e)
            await asyncio.sleep(TRAFFIC_SAMPLE_INTERVAL)

    hb_task = asyncio.create_task(heartbeat_loop())
    sample_task = asyncio.create_task(traffic_sample_loop())
    # DNS 探测队列的常驻消费者：await 队列，有数据入队即刻探测（内部自收在跑任务）
    probe_task = asyncio.create_task(dns_probe.run()) if dns_probe is not None else None
    loop = asyncio.get_running_loop()

    # 退出信号处理：Linux/systemd/docker 发 SIGTERM 时完成 stop_future 触发优雅退出
    # （Windows 的 add_signal_handler 不支持 SIGTERM，SIGINT 仍走 KeyboardInterrupt 路径）
    stop_future = loop.create_future()

    def _request_shutdown() -> None:
        """请求优雅退出：完成 stop_future，主循环 finally 走清理"""
        if not stop_future.done():
            stop_future.set_result(None)

    try:
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    except (NotImplementedError, RuntimeError):
        pass  # Windows 平台不支持 SIGTERM 信号处理

    try:
        await stop_future  # 永久运行（收到退出信号时完成）
    finally:
        # 清理：停止心跳/采样/探测/补测任务 → 关闭服务器 → 关闭 async session → 保存
        hb_task.cancel()
        sample_task.cancel()
        if probe_task is not None:
            probe_task.cancel()
        for t in retry_tasks.values():
            t.cancel()
        await asyncio.gather(hb_task, sample_task, *([probe_task] if probe_task is not None else []),
                             *retry_tasks.values(), return_exceptions=True)
        try:
            await server.close()
        except Exception as e:
            logger.warning("关闭服务器异常: %s", e)
        try:
            await c.async_getter.close()
        except Exception as e:
            logger.warning("关闭 async session 异常: %s", e)
        if panel is not None:
            await panel.close()
        # 退出前把待落盘的历史流量增量写入（不足 flush_interval 的那一段不丢）
        traffic.flush(time.time(), force=True)
        c.save_full()


def run(
    c,
    *,
    host: str,
    port: int,
    capacity: int,
    interval: int,
    probe_url: str,
    allowed_types: set[str] | None = None,
    concurrency: int,
    web_host: str | None = None,
    web_port: int | None = None,
    web_enabled: bool = True,
) -> None:
    """同步入口：构建并运行事件循环（Ctrl+C 干净退出）

    不传 allowed_types 时使用配置默认（[server].proxy_types），与 CLI 缺省行为一致；
    运行时实际生效集合由 ServeControl 持有（面板可增删）。
    """
    types = set(allowed_types) if allowed_types else set(config.SERVER_PROXY_TYPES)
    try:
        run_coro(_run(
            c,
            interval=interval,
            probe_url=probe_url,
            allowed_types=types,
            concurrency=concurrency,
            host=host,
            port=port,
            capacity=capacity,
            web_host=web_host,
            web_port=web_port,
            web_enabled=web_enabled,
        ))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号，服务器已停止")
