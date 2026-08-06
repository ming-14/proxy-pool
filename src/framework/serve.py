"""serve 命令主循环：本地代理服务器 + 备用队列维护

备用队列组（配置 role="standby" 的组）作为 pproxy 上游（有界，容量见
STANDBY_QUEUE_CAPACITY）。队列维护业务（探测补入/心跳淘汰/退避补测）
在 StandbyQueueUseCase（use_cases 层），本模块只负责：
    调度：启动填充 → 周期心跳 → 为待补测代理启动独立复核任务 → 同步上游；
    生命周期：启动/停止事件循环。
"""

import asyncio
import logging
import time

from src.framework import config
from src.interfaces.proxy_server import PproxyServer

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
) -> None:
    """服务器主循环：启动 + 周期心跳 + 队列补充 + 退避补测"""
    server = PproxyServer(listen_uri=f"http+socks4+socks5://{host}:{port}")
    standby = c.standby

    def sync_and_save() -> None:
        """同步 pproxy 上游列表并持久化（队列成员每次变更后调用）"""
        server.sync(standby.standby_proxies())
        c.save()

    async def fill_if_needed() -> None:
        """缺口补入并同步上游（fill 期间实时 sync，结束后统一 sync + save）

        on_change=server.sync 让 fill 流式入队时每个代理入队即同步 pproxy 上游，
        探测成功的代理立即可用，不必等整批探测完成。
        """
        if len(standby.standby_proxies()) < capacity:
            await standby.fill(allowed_types, probe_url, config.HEARTBEAT_TIMEOUT, concurrency, capacity,
                               on_change=server.sync)
        sync_and_save()

    # 启动：探测补入备用队列并启动服务器
    await standby.fill(allowed_types, probe_url, config.HEARTBEAT_TIMEOUT, concurrency, capacity)
    await server.start(standby.standby_proxies())
    c.save()
    if standby.standby_proxies():
        logger.info("服务器就绪: 上游 %d 个代理", len(standby.standby_proxies()))
    else:
        logger.warning("警告: 备用队列仍无可用 %s 类型代理", ",".join(sorted(allowed_types)))

    # 退避补测任务表：proxy -> asyncio.Task（失败代理独立复核，完成即清出）
    retry_tasks: dict[str, asyncio.Task] = {}

    async def retry_with_sync(p: str) -> None:
        """补测完成后同步上游（成功恢复/达到阈值拉黑都会改变队列成员）"""
        try:
            await standby.retry_probe(p, probe_url, config.HEARTBEAT_TIMEOUT)
        finally:
            sync_and_save()

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
                if standby.standby_proxies():
                    await standby.heartbeat(probe_url, config.HEARTBEAT_TIMEOUT, concurrency)
                await fill_if_needed()
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
            next_round = max(time.monotonic(), round_start + interval)

    hb_task = asyncio.create_task(heartbeat_loop())
    try:
        await asyncio.Future()  # 永久运行，直到被取消
    finally:
        # 清理：停止心跳与补测任务 → 关闭服务器 → 保存
        hb_task.cancel()
        for t in retry_tasks.values():
            t.cancel()
        await asyncio.gather(hb_task, *retry_tasks.values(), return_exceptions=True)
        try:
            await server.close()
        except Exception as e:
            logger.warning("关闭服务器异常: %s", e)
        c.save()


def run(
    c,
    *,
    host: str,
    port: int,
    capacity: int,
    interval: int = 60,
    probe_url: str = "https://httpbin.org/ip",
    allowed_types: set[str] | None = None,
    concurrency: int = 16,
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
        ))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号，服务器已停止")
