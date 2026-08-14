"""serve 命令主循环：本地代理服务器 + 代理服务队列维护

代理服务队列组（配置 role="standby" 的组）作为 pproxy 上游（有界，容量见
STANDBY_QUEUE_CAPACITY）。队列维护业务（探测补入/心跳淘汰/退避补测）
在 StandbyQueueUseCase（use_cases 层），本模块只负责：
    调度：启动填充 → 周期心跳 → 为待补测代理启动独立复核任务 → 同步上游；
    生命周期：启动/停止事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from src.framework import config
from src.interfaces.proxy_server import PproxyServer
from src.framework.web import ServeControl, WebPanel
from src.framework.web.api import PanelContext

logger = logging.getLogger(__name__)


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
) -> None:
    """服务器主循环：启动 + 周期心跳 + 队列补充 + 退避补测 + Web 控制面板"""
    server = PproxyServer(listen_uri=f"http+socks4+socks5://{host}:{port}",
                          metrics_window=config.METRICS_WINDOW)
    standby = c.standby
    standby.set_capacity(capacity)  # 队列容量：standby_proxies 截断超出容量的固定代理
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

    # save 节流：每 30 秒持久化一次（c.save 序列化全池 + 文件 I/O，每 2 秒一轮太频繁）
    _last_save = 0.0
    _SAVE_INTERVAL = 30.0

    def sync_upstream() -> None:
        """同步 pproxy 上游列表（轻量，每轮调用）"""
        server.sync(standby.upstream_proxies())

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

        on_change 回调让 fill 流式入队时每个代理入队即同步 pproxy 上游，
        探测成功的代理立即可用，不必等整批探测完成。
        """
        if len(standby.standby_proxies()) < capacity:
            await standby.fill(control.allowed_types, control.probe_url, control.timeout,
                               control.concurrency, capacity,
                               on_change=lambda: server.sync(standby.upstream_proxies()))
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
    panel = WebPanel(PanelContext(
        pool=c.pool,
        fetch=c.fetch,
        record=c.record,
        standby=standby,
        control=control,
        server=server,
        save=c.save,
        capacity=capacity,
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

    async def heartbeat_loop():
        """周期心跳：轮次绝不重叠，间隔至少 interval

        探测（fill/heartbeat）为 async（内部 to_thread 投递网络请求），
        事件循环保持响应 pproxy 请求；下一轮最早从「本轮开始时刻 + interval」
        起算（且不小于当前时刻），即两轮间隔 >= interval，轮内耗时超过
        interval 时立即开始下一轮，避免连续探测刷屏，也不累积 sleep 计时误差。
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
                # 有上游代理才心跳：锁定时为锁定代理（测速），否则为代理服务队列成员
                if standby.upstream_proxies():
                    await standby.heartbeat(control.probe_url, control.timeout, control.concurrency)
                await fill_if_needed()
                server.sample_traffic()
                # 为失败后进入退避补测的代理启动独立复核任务（已启动的跳过），
                # 复核成功恢复 / 达到阈值拉黑，期间不占心跳轮次。
                for p in standby.pending_retries():
                    if p not in retry_tasks:
                        retry_tasks[p] = asyncio.create_task(retry_with_sync(p))
                for p in list(retry_tasks):
                    if retry_tasks[p].done():
                        del retry_tasks[p]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("心跳循环异常: %s", e)
            # 下一轮最早从本轮开始时刻 + interval 起算（间隔至少 interval，绝不重叠）
            next_round = max(time.monotonic(), round_start + control.interval)

    hb_task = asyncio.create_task(heartbeat_loop())
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
        # 清理：停止心跳与补测任务 → 关闭服务器 → 关闭 async session → 保存
        hb_task.cancel()
        for t in retry_tasks.values():
            t.cancel()
        await asyncio.gather(hb_task, *retry_tasks.values(), return_exceptions=True)
        try:
            await server.close()
        except Exception as e:
            logger.warning("关闭服务器异常: %s", e)
        try:
            await c.async_getter.close()
        except Exception as e:
            logger.warning("关闭 async session 异常: %s", e)
        if web_port:
            await panel.close()
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
) -> None:
    """同步入口：构建并运行事件循环（Ctrl+C 干净退出）"""
    types = allowed_types or {"http", "https", "socks5"}
    try:
        asyncio.run(_run(
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
        ))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号，服务器已停止")
