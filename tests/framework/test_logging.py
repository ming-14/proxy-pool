"""config/logging.toml 驱动的日志装配：双通道级别分离 + 逐 logger 覆盖 + --debug 强制"""
import logging
import os
import unittest
from unittest.mock import patch

from src.framework import config
from src.framework.logging import setup_logging


class _SetupLoggingBase(unittest.TestCase):
    """setup_logging 单次全局初始化：每个用例重置守卫与根 handler，LOG_DIR 指到临时目录"""

    def setUp(self):
        import src.framework.logging as logmod
        logmod._initialized = False
        self.root = logging.getLogger()
        self.saved_handlers = list(self.root.handlers)
        self.saved_level = self.root.level
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_tmp_logs")
        self._patchers = [
            patch.object(config, "LOG_DIR", self.tmpdir),
            patch.object(config, "LOG_CONSOLE_LEVEL", logging.DEBUG),
            patch.object(config, "LOG_FILE_LEVEL", logging.INFO),
            patch.object(config, "LOG_LOGGERS", {"aiohttp.access": logging.WARNING}),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        for h in list(self.root.handlers):
            if h not in self.saved_handlers:
                h.close()
                self.root.removeHandler(h)
        self.root.setLevel(self.saved_level)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        import src.framework.logging as logmod
        logmod._initialized = False

    def _handlers(self):
        return [h for h in self.root.handlers if h not in self.saved_handlers]


class TestSetupLogging(_SetupLoggingBase):
    def test_channel_levels(self):
        """控制台/文件级别独立分档（DEBUG/INFO）"""
        setup_logging()
        hs = self._handlers()
        stream = [h for h in hs if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
        file_ = [h for h in hs if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(stream), 1)
        self.assertEqual(len(file_), 1)
        self.assertEqual(stream[0].level, logging.DEBUG)
        self.assertEqual(file_[0].level, logging.INFO)

    def test_debug_overrides_both(self):
        """--debug 双通道强制 DEBUG，无视配置分档"""
        setup_logging(debug=True)
        for h in self._handlers():
            self.assertEqual(h.level, logging.DEBUG)

    def test_logger_overrides_applied(self):
        """loggers 表逐 logger 覆盖生效（aiohttp.access=WARNING）"""
        setup_logging()
        self.assertEqual(logging.getLogger("aiohttp.access").level, logging.WARNING)


class TestLevelParsing(unittest.TestCase):
    def test_invalid_level_raises(self):
        with self.assertRaises(ValueError):
            config._log_level("file_level", "VERBOSE", "file_level")

    def test_case_insensitive(self):
        self.assertEqual(config._log_level("x", "warning", "x"), logging.WARNING)


class TestRealConfigFile(unittest.TestCase):
    def test_shipped_logging_toml(self):
        """仓库自带的 config/logging.toml 可解析且分档为控制台 DEBUG / 文件 INFO"""
        import tomllib
        path = os.path.join(os.path.dirname(config.__file__), "..", "..", "config", "logging.toml")
        with open(os.path.abspath(path), "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["logging"]["console_level"], "DEBUG")
        self.assertEqual(data["logging"]["file_level"], "INFO")
        self.assertEqual(data["logging"]["loggers"]["aiohttp.access"], "WARNING")


if __name__ == "__main__":
    unittest.main()
