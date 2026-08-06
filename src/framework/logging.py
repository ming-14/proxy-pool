"""日志配置：统一格式（含毫秒时间戳），输出到 stderr + logs 目录文件

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


def setup_logging(level=logging.INFO):
    """配置根日志器：stderr + 文件（文件名精确到毫秒），仅初始化一次"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = _build_log_file()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(_MsecFormatter(LOG_FORMAT))

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_MsecFormatter(LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
