"""代理筛选规则：对完整代理 URL 做正则匹配，命中即拉黑（纯匹配，零 IO）"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class FilterRule:
    """一条筛选规则：pattern 命中即拉黑，source_glob 限定生效的源"""

    pattern: re.Pattern[str]
    reason: str
    source_glob: str = "*"

    def matches(self, source: str, proxy: str) -> bool:
        return (fnmatch.fnmatchcase(source, self.source_glob)
                and self.pattern.search(proxy) is not None)


def match_rule(rules: Iterable[FilterRule], source: str, proxy: str) -> FilterRule | None:
    """按声明顺序返回首个命中的规则，无命中返回 None"""
    for rule in rules:
        if rule.matches(source, proxy):
            return rule
    return None
