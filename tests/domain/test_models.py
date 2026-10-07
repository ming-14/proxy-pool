"""domain.models.pick_supported_scheme：上游多协议条目 -> 池内单一 scheme 的取舍策略"""

import unittest

from src.domain.models import SCHEME_PRIORITY, pick_supported_scheme


class TestPickSupportedScheme(unittest.TestCase):
    """一个代理只入池一条，必须从上游协议列表里选一个受支持 scheme"""

    def test_priority_http_first(self):
        """http 优先：http 代理经 CONNECT 同时可访问 http/https 目标，兼容面最广"""
        self.assertEqual(SCHEME_PRIORITY, ("http", "https", "socks5"))
        self.assertEqual(pick_supported_scheme(["socks5", "http"]), "http")
        self.assertEqual(pick_supported_scheme(["https", "socks5"]), "https")

    def test_case_and_whitespace_insensitive(self):
        """上游大小写不一（proxylister 给 HTTP、imtaqin 给 SOCKS5）"""
        self.assertEqual(pick_supported_scheme(["HTTP"]), "http")
        self.assertEqual(pick_supported_scheme([" SOCKS5 "]), "socks5")

    def test_socks4_never_selected(self):
        """socks4 不在 SUPPORTED_SCHEMES，混在列表里只能被跳过"""
        self.assertEqual(pick_supported_scheme(["socks4", "socks5"]), "socks5")
        self.assertIsNone(pick_supported_scheme(["socks4"]))
        self.assertIsNone(pick_supported_scheme(["shadowsocks", "trojan"]))

    def test_empty_input(self):
        self.assertIsNone(pick_supported_scheme([]))
        self.assertIsNone(pick_supported_scheme((None,)))  # 协议字段缺失


if __name__ == "__main__":
    unittest.main()
