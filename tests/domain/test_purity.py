"""纯净度领域契约单测：代理规则匹配与结果归一化（纯函数）"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.domain.purity import ProxyRule, UsageType, normalize_result, resolve_proxy


class ResolveProxyTest(unittest.TestCase):
    """代理规则：按声明顺序取首个 match 命中的 glob"""

    def test_exact_match(self):
        rules = [ProxyRule(match="abuseipdb", use="http://127.0.0.1:7890")]
        assert resolve_proxy(rules, "abuseipdb") == "http://127.0.0.1:7890"

    def test_glob_prefix(self):
        rules = [ProxyRule(match="abuse*", use="http://127.0.0.1:7890")]
        assert resolve_proxy(rules, "abuseipdb") == "http://127.0.0.1:7890"

    def test_first_match_wins(self):
        rules = [
            ProxyRule(match="abuse*", use="http://127.0.0.1:7890"),
            ProxyRule(match="*", use="socks5://127.0.0.1:1080"),
        ]
        assert resolve_proxy(rules, "abuseipdb") == "http://127.0.0.1:7890"
        assert resolve_proxy(rules, "ipok") == "socks5://127.0.0.1:1080"

    def test_no_match_means_direct(self):
        rules = [ProxyRule(match="abuse*", use="http://127.0.0.1:7890")]
        assert resolve_proxy(rules, "ipok") == ""

    def test_empty_use_means_direct(self):
        """显式声明走直连（覆盖更宽的兜底规则）"""
        rules = [
            ProxyRule(match="ipok", use=""),
            ProxyRule(match="*", use="http://127.0.0.1:7890"),
        ]
        assert resolve_proxy(rules, "ipok") == ""
        assert resolve_proxy(rules, "other") == "http://127.0.0.1:7890"

    def test_no_rules(self):
        assert resolve_proxy((), "abuseipdb") == ""

    def test_glob_is_case_sensitive(self):
        assert resolve_proxy([ProxyRule(match="Abuse*", use="http://x")], "abuseipdb") == ""


class NormalizeResultTest(unittest.TestCase):
    """插件结果 → 通用契约 {score, usage_type, extra}"""

    def setUp(self):
        # 本套件其它测试模块在导入期 logging.disable(WARNING)（进程级生效），断言日志前须恢复
        logging.disable(logging.NOTSET)

    def tearDown(self):
        logging.disable(logging.WARNING)

    def test_valid_result(self):
        out = normalize_result({"score": 35, "usage_type": UsageType.RESIDENTIAL,
                                "extra": {"isp": "X"}})
        assert out == {"score": 35, "usage_type": "residential", "extra": {"isp": "X"}}

    def test_usage_enum_value_string_accepted(self):
        out = normalize_result({"score": 0, "usage_type": "hosting", "extra": {}})
        assert out["usage_type"] == "hosting"

    def test_missing_fields(self):
        out = normalize_result({})
        assert out == {"score": None, "usage_type": "unknown", "extra": {}}

    def test_score_bounds(self):
        assert normalize_result({"score": 100})["score"] == 100
        assert normalize_result({"score": 0})["score"] == 0

    def test_score_out_of_range_becomes_none(self):
        with self.assertLogs("src.domain.purity", level="WARNING"):
            assert normalize_result({"score": 101})["score"] is None
            assert normalize_result({"score": -1})["score"] is None

    def test_score_non_int_becomes_none(self):
        with self.assertLogs("src.domain.purity", level="WARNING"):
            assert normalize_result({"score": "35"})["score"] is None
            assert normalize_result({"score": True})["score"] is None

    def test_unmapped_usage_becomes_unknown(self):
        with self.assertLogs("src.domain.purity", level="WARNING"):
            assert normalize_result({"usage_type": "satellite"})["usage_type"] == "unknown"

    def test_extra_non_dict_becomes_empty(self):
        with self.assertLogs("src.domain.purity", level="WARNING"):
            assert normalize_result({"extra": [1, 2]})["extra"] == {}

    def test_none_values_are_not_treated_as_dirty(self):
        """None 表示源未给出，属正常（不记警告）"""
        out = normalize_result({"score": None, "usage_type": None, "extra": None})
        assert out == {"score": None, "usage_type": "unknown", "extra": {}}


if __name__ == "__main__":
    unittest.main()
