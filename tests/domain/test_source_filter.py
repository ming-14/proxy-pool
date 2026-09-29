"""入池筛选规则单测：正则匹配、源 glob 限定、首个命中、规则顺序

纯领域层逻辑，不加载配置（配置侧的校验用例在 tests/framework/test_config.py）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.source_filter import FilterRule, match_rule

SRC = "src.interfaces.sources.geonode"
FPW = "src.interfaces.sources.freeproxy_world"


def _rule(pattern: str, *, match: str = "", reason: str = "r") -> FilterRule:
    return FilterRule(pattern=re.compile(pattern), reason=reason, source_glob=match or "*")


class TestPatternMatching:
    """正则对完整代理 URL 全串匹配（re.search 语义）"""

    def test_port_80_matches(self):
        rule = _rule(":80$")
        assert rule.matches(SRC, "http://1.2.3.4:80")

    def test_port_8080_not_matched_by_80_rule(self):
        """$ 锚定行尾：:80 不得命中 :8080"""
        rule = _rule(":80$")
        assert not rule.matches(SRC, "http://1.2.3.4:8080")

    def test_scheme_match(self):
        rule = _rule("^http://")
        assert rule.matches(SRC, "http://1.2.3.4:8080")
        assert not rule.matches(SRC, "https://1.2.3.4:8080")

    def test_ip_segment_match(self):
        rule = _rule(r"://(10|127|192\.168)\.")
        assert rule.matches(SRC, "http://10.1.2.3:3128")
        assert rule.matches(SRC, "socks5://192.168.1.1:1080")
        assert not rule.matches(SRC, "http://11.1.2.3:3128")

    def test_scheme_and_port_combined(self):
        """正则内即 AND：scheme 与端口可同时约束"""
        rule = _rule("^https://.*:8080$")
        assert rule.matches(SRC, "https://1.2.3.4:8080")
        assert not rule.matches(SRC, "http://1.2.3.4:8080")

    def test_search_semantics_not_fullmatch(self):
        """局部规则无需补 .* 前缀（re.search）"""
        assert _rule("2\\.3\\.").matches(SRC, "http://1.2.3.4:8080")


class TestSourceGlob:
    """match 限定规则生效的源"""

    def test_glob_limits_scope(self):
        rule = _rule(":80$", match="src.interfaces.sources.geonode")
        assert rule.matches(SRC, "http://1.2.3.4:80")
        assert not rule.matches(FPW, "http://1.2.3.4:80")

    def test_wildcard_glob(self):
        rule = _rule(":80$", match="src.interfaces.sources.*")
        assert rule.matches(SRC, "http://1.2.3.4:80")
        assert rule.matches(FPW, "http://1.2.3.4:80")

    def test_empty_glob_is_normalized_to_star(self):
        """省略 match 时构造函数收到的就是 "*"，对所有源生效"""
        rule = _rule(":80$")
        assert rule.matches(SRC, "http://1.2.3.4:80")
        assert rule.matches(FPW, "http://1.2.3.4:80")


class TestMatchRule:
    """按声明顺序取首个命中"""

    def test_first_match_wins(self):
        rules = (
            _rule(":80$", match=SRC, reason="专用规则"),
            _rule(":80$", reason="通用规则"),
        )
        hit = match_rule(rules, SRC, "http://1.2.3.4:80")
        assert hit is not None and hit.reason == "专用规则"

    def test_falls_through_to_later_rule(self):
        """前面的规则源不匹配时，继续看后面的规则"""
        rules = (
            _rule(":80$", match=FPW, reason="仅 FPW"),
            _rule(":80$", reason="通用"),
        )
        hit = match_rule(rules, SRC, "http://1.2.3.4:80")
        assert hit is not None and hit.reason == "通用"

    def test_no_match_returns_none(self):
        assert match_rule((_rule(":80$"),), SRC, "http://1.2.3.4:8080") is None

    def test_empty_rules_returns_none(self):
        assert match_rule((), SRC, "http://1.2.3.4:80") is None
