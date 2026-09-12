"""面板日志缓冲：root logger 输出镜像到有界环形缓冲，供 Web 面板增量拉取

- PanelLogHandler 在 setup_logging() 内挂到 root logger，级别 DEBUG——
  与 stderr「控制台」通道一致；[logging.loggers] 逐 logger 覆盖在 logger 级
  生效，缓冲内容天然等同于控制台输出。
- entry 带单调递增 seq，前端以 since=seq 增量轮询，响应只含新条目；
  容量 2000 有界（与前端 LogPanel 缓冲容量约定一致），超限丢弃最旧。
  异常记录（record.exc_info）附加 stack 字段，面板可展开堆栈。
- snapshot 同时返回 buffer_start（缓冲最旧条目 seq）：serve 重启后 seq
  从 1 重新计数或缓冲淘汰越过游标时，前端据此检测增量失效并全量重拉。
- 线程安全：日志可能来自任意线程（aiohttp 请求、asyncio.to_thread 探测），
  缓冲读写互斥保护；格式化在锁外完成，锁内只做入队。
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque

# 缓冲容量：与前端 frontend/src/vendor/logpanel 视图容量约定一致
PANEL_LOG_CAPACITY = 2000

# logging 级别 → LogPanel level（0=DEBUG 1=INFO 2=WARN 3=ERROR；CRITICAL 归入 ERROR 档）
_LEVEL_INDEX = {
    logging.DEBUG: 0,
    logging.INFO: 1,
    logging.WARNING: 2,
    logging.ERROR: 3,
    logging.CRITICAL: 3,
}


def _level_index(levelno: int) -> int:
    """日志级别映射为 LogPanel 等级索引（未识别级别按最邻近档位）"""
    return _LEVEL_INDEX.get(levelno, 3 if levelno >= logging.ERROR else 1)


class PanelLogHandler(logging.Handler):
    """把日志记录镜像为 {seq, ts, level, levelName, tag, text} 推入有界环形缓冲"""

    def __init__(self, capacity: int = PANEL_LOG_CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self._buffer: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        """构造 entry 并入队（格式化在锁外，避免持锁执行用户代码）"""
        try:
            stack = None
            if record.exc_info:
                stack = "".join(traceback.format_exception(*record.exc_info))
            entry = {
                "seq": None,  # 占位，入队时在锁内分配
                "ts": int(record.created * 1000),
                "level": _level_index(record.levelno),
                "levelName": record.levelname,
                "tag": record.name,
                "text": record.getMessage(),
                "stack": stack,
            }
        except Exception:
            # 记录损坏时不中断日志链路（镜像缓冲为辅助能力，失败静默）
            return
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buffer.append(entry)

    def snapshot(self, since: int = 0) -> tuple[list[dict], int, int]:
        """返回 seq > since 的条目、最新 seq、缓冲最旧条目 seq（空缓冲为 0）

        buffer_start 供前端检测增量失效：请求游标小于 buffer_start（缓冲已
        淘汰越过游标）或新 cursor 小于请求游标（serve 重启 seq 回绕）时，
        增量不完整，须以 since=0 全量重拉。
        """
        with self._lock:
            start = self._buffer[0]["seq"] if self._buffer else 0
            entries = [e for e in self._buffer if e["seq"] > since]
            return entries, self._seq, start

    def clear(self) -> None:
        """清空缓冲（不影响 stderr/文件通道）"""
        with self._lock:
            self._buffer.clear()

    def get_size(self) -> int:
        with self._lock:
            return len(self._buffer)


# 进程级单例：serve 全生命周期唯一，Web 面板与日志初始化共享
_handler: PanelLogHandler | None = None


def get_buffer() -> PanelLogHandler:
    """获取面板日志缓冲（懒安装，幂等）"""
    global _handler
    if _handler is None:
        _handler = PanelLogHandler()
        logging.getLogger().addHandler(_handler)
    return _handler