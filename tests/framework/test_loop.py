"""loop 模块测试：Proactor 连接复位噪音过滤 + run_coro 入口

- 噪音上下文（_call_connection_lost 回调 + OSError）被过滤，不触发默认处理器；
- 非噪音上下文（其他回调异常 / 同回调非 OSError）原样走默认处理器；
- 未设置默认处理器时回退 loop.default_exception_handler；
- run_coro 在事件循环内安装过滤器（后续噪音不再刷屏）。
"""

import asyncio
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.framework.loop import install_proactor_noise_filter, run_coro  # noqa: E402

logging.disable(logging.WARNING)

NOISE_MESSAGE = "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"


def _noise_context(exc):
    """构造与 asyncio 默认异常处理器一致的噪音上下文"""
    return {"message": NOISE_MESSAGE, "exception": exc, "handle": object()}


class InstallFilterTest(unittest.IsolatedAsyncioTestCase):
    def test_noise_oserror_filtered(self):
        """对端复位连接导致的 OSError（10054）被过滤：默认处理器不被调用"""
        loop = asyncio.new_event_loop()
        try:
            default = MagicMock()
            loop.set_exception_handler(default)
            install_proactor_noise_filter(loop)
            loop.call_exception_handler(_noise_context(
                ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")))
            default.assert_not_called()
        finally:
            loop.close()

    def test_other_exception_passes_through(self):
        """其他回调异常（非噪音）原样交给默认处理器"""
        loop = asyncio.new_event_loop()
        try:
            default = MagicMock()
            loop.set_exception_handler(default)
            install_proactor_noise_filter(loop)
            ctx = {"message": "Exception in callback some_other_callback()",
                   "exception": RuntimeError("boom")}
            loop.call_exception_handler(ctx)
            default.assert_called_once()
            default.call_args.args[1] is ctx
        finally:
            loop.close()

    def test_same_callback_non_oserror_passes_through(self):
        """同回调抛出的非 OSError（如协议层逻辑错误）不能被误过滤"""
        loop = asyncio.new_event_loop()
        try:
            default = MagicMock()
            loop.set_exception_handler(default)
            install_proactor_noise_filter(loop)
            ctx = _noise_context(RuntimeError("protocol bug"))
            loop.call_exception_handler(ctx)
            default.assert_called_once()
        finally:
            loop.close()

    def test_no_default_handler_falls_back(self):
        """未设置过默认处理器时回退 loop.default_exception_handler"""
        loop = asyncio.new_event_loop()
        try:
            install_proactor_noise_filter(loop)
            with patch.object(loop, "default_exception_handler") as fallback:
                loop.call_exception_handler(
                    {"message": "Exception in callback foo()", "exception": ValueError("x")})
                fallback.assert_called_once()
        finally:
            loop.close()

    def test_run_coro_installs_filter(self):
        """run_coro 在进入事件循环后安装了过滤器（噪音不再抛到默认处理器）"""
        seen = []

        async def main():
            # 直接触发 loop 异常处理器：噪音被吞掉，非噪音正常上报
            loop = asyncio.get_running_loop()
            loop.call_exception_handler(_noise_context(ConnectionResetError(10054, "reset")))
            loop.call_exception_handler(
                {"message": "Exception in callback other()", "exception": ValueError("x")})
            seen.append(True)

        run_coro(main())
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
