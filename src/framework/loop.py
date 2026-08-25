"""事件循环工具：Windows Proactor 关闭期连接复位噪音过滤

CPython 3.11 proactor_events.py:165 的 _ProactorBasePipeTransport._call_connection_lost
在 self._sock.shutdown(SHUT_RDWR) 处无 try/except：对端（浏览器/上游代理/探测目标）
已 RST 或关闭的连接，回调执行时 shutdown() 抛 ConnectionResetError [WinError 10054]
（远程主机强迫关闭了现有的连接），被 asyncio 默认异常处理器打印成
"Exception in callback ..." ERROR 刷屏——退出清场时每条连接各报一次，运行期
上游代理 RST 空闲连接时偶发。该缺陷在 stdlib 中无法就地修复（不随应用分发），
本模块安装精确过滤该已知噪音的异常处理器：仅匹配该回调抛出的 OSError，
其余异常（含同回调抛出的非 OSError）原样交给默认处理器。
serve/test 命令统一经 run_coro 进入事件循环，自动带上过滤器。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# 噪音回调标识：默认异常处理器消息 "Exception in callback <qualname>(...)"，
# qualname 取自定义处类名（asyncio.format_helpers._format_callback），各版本稳定
_NOISE_CALLBACK = "_ProactorBasePipeTransport._call_connection_lost"


def install_proactor_noise_filter(loop: asyncio.AbstractEventLoop) -> None:
    """安装异常处理器：仅过滤 _call_connection_lost 回调里的连接复位 OSError

    命中时记 DEBUG 日志（信息不丢失，只是不再 ERROR 刷屏）；
    未命中（含其他回调异常、同回调抛出的非 OSError）原样走原处理器/默认处理器。
    """
    default_handler = loop.get_exception_handler()

    def handler(loop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        message = context.get("message", "")
        if isinstance(exc, OSError) and _NOISE_CALLBACK in message:
            logger.debug("忽略 Proactor 连接复位噪音: %s: %s", type(exc).__name__, exc)
            return
        if default_handler is not None:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def run_coro(coro: Coroutine) -> Any:
    """asyncio.run 封装：进入事件循环后立即安装 Proactor 噪音过滤

    所有 asyncio.run 调用点（serve/test 命令）统一走此入口，
    保证 Windows 下关闭/运行期的连接复位噪音被过滤（见模块 docstring）。
    """

    async def _guarded() -> Any:
        install_proactor_noise_filter(asyncio.get_running_loop())
        return await coro

    return asyncio.run(_guarded())
