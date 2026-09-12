"""日志配置：统一格式（毫秒时间戳），双通道输出，级别由 config/logging.toml 决定

- 控制台（stderr）与文件（logs/）级别独立分档（默认 DEBUG / INFO）：
  周期调度细节控制台实时可见，文件只留事件级避免刷盘
- [logging.loggers] 逐 logger 覆盖（如 aiohttp.access=WARNING 抑制面板轮询访问日志）
- --debug 忽略配置文件，双通道强制 DEBUG（完整调试落盘）

文件日志每次启动生成独立文件，文件名精确到毫秒（天然唯一，不覆盖旧日志）。
仅在 CLI 入口调用。
"""

import logging
import os
import sys
import time
from datetime import datetime

from src.framework import config
from src.framework.logbuffer import get_buffer

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


class _MsecFormatter(logging.Formatter):
    """日志时间戳格式化为毫秒精度：YYYY-MM-DD HH:MM:SS.mmm"""

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        t = time.strftime(LOG_DATE_FORMAT, ct)
        return f"{t}.{int(record.msecs):03d}"


def _build_log_file() -> str:
    """生成日志文件路径：logs/proxy-pool-YYYYMMDD-HHMMSS-mmm.log（毫秒精度）"""
    now = datetime.now()
    filename = f"proxy-pool-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}.log"
    return os.path.join(config.LOG_DIR, filename)


def setup_logging(debug: bool = False):
    """配置根日志器（仅初始化一次）

    Args:
        debug: True 时控制台与文件均强制 DEBUG；否则按 logging.toml 的
               console_level / file_level 分档，loggers 表逐 logger 覆盖。
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    console_level = logging.DEBUG if debug else config.LOG_CONSOLE_LEVEL
    file_level = logging.DEBUG if debug else config.LOG_FILE_LEVEL

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = _build_log_file()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(_MsecFormatter(LOG_FORMAT))
    file_handler.setLevel(file_level)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_MsecFormatter(LOG_FORMAT))
    stream_handler.setLevel(console_level)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 根级别取最低，handler 各自过滤
    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    # 面板日志缓冲：与控制台同级别（DEBUG），供 Web 面板增量拉取（见 logbuffer.py）
    get_buffer()

    # 逐 logger 覆盖：logger 级过滤与 handler 级取交集（两边都放行才输出）
    for name, level in config.LOG_LOGGERS.items():
        logging.getLogger(name).setLevel(level)
