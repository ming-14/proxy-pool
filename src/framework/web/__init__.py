"""Web 控制面板（serve 模式）：监控 + 控制 + 调度策略运行时调整

框架层子包：control（ServeControl 运行时参数）、api（REST 路由）、
server（aiohttp 挂载）、templates（单页前端）。
"""

from src.framework.web.control import ServeControl
from src.framework.web.server import WebPanel

__all__ = ["ServeControl", "WebPanel"]
