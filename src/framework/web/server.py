"""Web 控制面板 HTTP 服务：aiohttp 挂载到 serve 现有事件循环（零额外线程）

serve 主循环内调用 WebPanel.start() 把 AppRunner + TCPSite 启动在同一个
asyncio loop 上；serve 退出时 close() 清理。WebPanel 持有共享的
PanelContext（与 serve 同源），保证监控读数与控制动作操作同一份状态。
"""

from __future__ import annotations

import logging

from aiohttp import web

from src.framework.web.api import PanelContext, build_app

logger = logging.getLogger(__name__)


class WebPanel:
    """aiohttp 控制面板服务器封装"""

    def __init__(self, ctx: PanelContext) -> None:
        self._ctx = ctx
        self._app = build_app(ctx)
        self._runner: web.AppRunner | None = None

    async def start(self, host: str, port: int) -> None:
        """启动面板（绑定 host:port；须在 serve 事件循环内调用）"""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        logger.info("Web 控制面板已启动: http://%s:%d", host, port)

    async def close(self) -> None:
        """停止面板并释放 aiohttp runner"""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web 控制面板已停止")
