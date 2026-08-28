"""日志配置：统一格式（含毫秒时间戳），输出到 stderr + logs 目录文件

控制台（stderr）与文件日志级别分开：
- 控制台默认 DEBUG（周期日志可见，便于观察调度细节；高频轮转日志不打文件）
- 文件默认 INFO（事件级，避免每 2s 心跳轮次刷屏磁盘）
- --debug 时文件也降到 DEBUG（完整调试）

文件日志每次启动生成独立文件，文件名精确到毫秒（天然唯一，不覆盖旧日志）。
仅在 CLI 入口调用。
"""

import logging
import os
import sys
import time
from datetime import datetime

from src.framework import config

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


def setup_logging(file_level=logging.INFO):
    """配置根日志器：控制台 DEBUG + 文件 file_level（仅初始化一次）

    Args:
        file_level: 文件日志级别（默认 INFO；--debug 时传 DEBUG 全量落盘）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = _build_log_file()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(_MsecFormatter(LOG_FORMAT))
    file_handler.setLevel(file_level)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_MsecFormatter(LOG_FORMAT))
    stream_handler.setLevel(logging.DEBUG)  # 控制台默认 DEBUG，观察周期调度细节

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 根级别取最低，handler 各自过滤
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
