"""面板日志缓冲（logbuffer.py）：镜像 root 日志 → 有界环形缓冲 + 增量拉取"""
import logging
import sys
import unittest

from src.framework.logbuffer import PanelLogHandler, PANEL_LOG_CAPACITY


def _record(levelno: int, name: str = "src.use_cases.demo", msg: str = "msg") -> logging.LogRecord:
    """构造最小 LogRecord（Python 3.8 的 exc_info 无默认值；跳过 logging.disable 等流程，直测 handler）"""
    return logging.LogRecord(name, levelno, __file__, 1, msg, (), None)


class TestPanelLogHandler(unittest.TestCase):
    def setUp(self):
        self.h = PanelLogHandler(capacity=10)

    def test_emit_snapshot_incremental(self):
        """snapshot(since) 只返回 seq 之后的条目；seq 单调递增；buffer_start 为最旧条目"""
        self.h.emit(_record(logging.INFO, msg="a"))
        self.h.emit(_record(logging.DEBUG, msg="b"))
        entries, cursor, start = self.h.snapshot()
        self.assertEqual(cursor, 2)
        self.assertEqual(start, 1)  # 缓冲最旧条目 seq
        self.assertEqual([e["seq"] for e in entries], [1, 2])
        self.assertEqual(entries[0]["text"], "a")
        self.assertEqual(entries[1]["text"], "b")
        since1, _, start1 = self.h.snapshot(1)
        self.assertEqual([e["seq"] for e in since1], [2])
        self.assertEqual(start1, 1)

    def test_entry_fields(self):
        """entry 结构：毫秒 ts / level 索引 / levelName / tag（logger 名）"""
        rec = _record(logging.WARNING, name="src.framework.serve", msg="boom")
        self.h.emit(rec)
        (e,) = self.h.snapshot()[0]
        self.assertEqual(e["tag"], "src.framework.serve")
        self.assertEqual(e["level"], 2)          # WARNING → LogPanel 2
        self.assertEqual(e["levelName"], "WARNING")
        self.assertEqual(e["text"], "boom")
        self.assertIsInstance(e["ts"], int)

    def test_level_mapping(self):
        """DEBUG→0 / INFO→1 / WARNING→2 / ERROR,CRITICAL→3"""
        for levelno, expect in [(logging.DEBUG, 0), (logging.INFO, 1),
                                (logging.WARNING, 2), (logging.ERROR, 3),
                                (logging.CRITICAL, 3)]:
            self.h.emit(_record(levelno))
        levels = [e["level"] for e in self.h.snapshot()[0]]
        self.assertEqual(levels, [0, 1, 2, 3, 3])

    def test_capacity_evicts_oldest(self):
        """环形缓冲超限丢弃最旧（seq 继续递增，淘汰只发生在队首）"""
        for i in range(12):
            self.h.emit(_record(logging.INFO, msg=f"m{i}"))
        entries, cursor, start = self.h.snapshot()
        self.assertEqual(len(entries), 10)
        self.assertEqual(cursor, 12)
        self.assertEqual(start, 3)              # 最旧条目 m2 的 seq
        self.assertEqual(entries[0]["text"], "m2")   # m0/m1 被淘汰
        self.assertEqual(entries[-1]["text"], "m11")

    def test_clear(self):
        self.h.emit(_record(logging.INFO))
        self.h.clear()
        self.assertEqual(self.h.get_size(), 0)
        self.assertEqual(self.h.snapshot()[0], [])
        self.assertEqual(self.h.snapshot()[2], 0)  # 空缓冲 buffer_start=0

    def test_exc_info_captured_as_stack(self):
        """带异常的记录附加 stack 字段（面板可展开堆栈）；无异常为 None"""
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        buf = PanelLogHandler(capacity=10)
        buf.emit(logging.LogRecord("src.x", logging.ERROR, __file__, 1, "failed", (), exc_info=exc_info))
        (e,) = buf.snapshot()[0]
        self.assertIsNotNone(e["stack"])
        self.assertIn("ValueError: boom", e["stack"])
        # 无异常记录 stack=None
        buf.emit(_record(logging.INFO))
        entries = buf.snapshot()[0]
        plain = next(x for x in entries if x["seq"] == 2)
        self.assertIsNone(plain["stack"])

    def test_broken_record_skipped(self):
        """损坏记录（getMessage 抛错）不中断链路、不入缓冲"""
        rec = logging.LogRecord("src.x", logging.INFO, __file__, 1, "msg %d", (object(),), None)
        self.h.emit(rec)
        self.assertEqual(self.h.get_size(), 0)

    def test_get_buffer_singleton_attaches_to_root(self):
        """get_buffer() 懒安装 + 幂等：单例且挂到 root logger"""
        from src.framework import logbuffer
        logbuffer._handler = None
        try:
            buf1 = logbuffer.get_buffer()
            buf2 = logbuffer.get_buffer()
            self.assertIs(buf1, buf2)
            self.assertIn(buf1, logging.getLogger().handlers)
        finally:
            # 还原并摘除，避免污染其它测试进程状态
            root = logging.getLogger()
            h = logbuffer._handler
            logbuffer._handler = None
            if h is not None and h in root.handlers:
                root.removeHandler(h)


if __name__ == "__main__":
    unittest.main()