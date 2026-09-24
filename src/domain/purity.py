"""IP 纯净度检查领域契约：通用结果字段、usage 枚举、插件元数据、声明式代理规则

核心（用例 / Web / 前端）只认本模块的通用契约，不出现任何具体检查源的名字、端点、
字段映射与配置键——源特定信息全部在 src/interfaces/ippure/ 内，各源地位平等。
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)


class UsageType(str, Enum):
    """IP 用途分类：插件把各自源的原值映射到本枚举，前端按枚举出统一短标签"""

    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    HOSTING = "hosting"
    BUSINESS = "business"
    EDUCATION = "education"
    GOVERNMENT = "government"
    MILITARY = "military"
    LIBRARY = "library"
    CDN = "cdn"
    SEARCH_ENGINE = "search_engine"
    RESERVED = "reserved"
    UNKNOWN = "unknown"


_USAGE_VALUES = frozenset(member.value for member in UsageType)


@dataclass(frozen=True)
class IppureSourceSpec:
    """插件元数据（模块级 SOURCE）

    name 同时是配置节名、缓存分区键与代理规则的匹配对象；env_keys 为插件所需环境变量
    （缺失时核心警告并禁用该源）；label 为展示名（启动日志与面板标注数据来源）。
    """

    name: str
    env_keys: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class IppurePluginConfig:
    """核心注入插件的运行时配置：插件不读 TOML 与环境变量，只用这里给的值

    section 为插件自声明配置节的原始表（缺省空表），env 为核心按 env_keys 解析好的密钥。
    """

    timeout: float
    proxy: str
    section: Mapping[str, object] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyRule:
    """声明式代理规则：match 为插件名 glob（* 通配），use 为代理 URL（空 = 直连）"""

    match: str
    use: str


def resolve_proxy(rules: Iterable[ProxyRule], source: str) -> str:
    """按声明顺序返回首个 match 命中的规则代理；无命中返回空串（直连）"""
    for rule in rules:
        if fnmatch.fnmatchcase(source, rule.match):
            return rule.use
    return ""


def normalize_result(raw: Mapping[str, object]) -> dict:
    """把插件返回收敛为通用契约 {score, usage_type, extra}

    score 为 0-100 整数（越高越脏），越界/非整数/缺失 → None；
    usage_type 为 UsageType 成员或其字符串值，非法 → unknown；
    extra 为源特有字段（非 dict → 空表）。脏值不抛错（数据问题不阻断功能），记 WARNING。
    """
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        if score is not None:
            logger.warning("纯净度分数非法，按未知处理: %r", score)
        score = None
    usage = raw.get("usage_type")
    value = usage.value if isinstance(usage, UsageType) else usage
    if value not in _USAGE_VALUES:
        if usage is not None:
            logger.warning("纯净度用途类型未归一化，按未知处理: %r", usage)
        value = UsageType.UNKNOWN.value
    extra = raw.get("extra")
    if not isinstance(extra, dict):
        if extra is not None:
            logger.warning("纯净度 extra 非字典，按空处理: %r", extra)
        extra = {}
    return {"score": score, "usage_type": value, "extra": extra}
