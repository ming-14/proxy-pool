"""入队筛选规则领域契约：声明式规则、优先级排序与纯判定

规则来自 serve.toml 的 [[server.enqueue_rules]]，按 priority 升序逐条判定，
未通过即短路（后续规则不执行）。本模块零 IO：只比较「已测得的指标值」与阈值，
不关心值从哪来、怎么测。

扩展点：新增指标 = METRIC_SPECS 加一条（参数校验 coerce + 纯判定 judge）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Verdict:
    """单条规则的判定结果"""
    passed: bool
    detail: str                  # 供日志的可读说明
    measured: bool = True        # False = 没取到值，按放过处理（缺数据不是代理的错）


@dataclass(frozen=True)
class MetricSpec:
    """指标定义：允许的参数键 + 参数校验 + 纯判定"""
    label: str
    required: frozenset[str]
    optional: frozenset[str]
    coerce: Callable[[Mapping[str, object]], dict]
    judge: Callable[[Mapping[str, object], object], Verdict]


@dataclass(frozen=True)
class EnqueueRule:
    """一条入队筛选规则"""
    metric: str
    priority: int                # 升序执行，数字小先测
    params: Mapping[str, object] = field(hash=False, default_factory=dict)
    enabled: bool = False        # 默认不启用，网页设置可运行时开启

    @property
    def label(self) -> str:
        """展示名（日志/面板）"""
        spec = METRIC_SPECS.get(self.metric)
        return spec.label if spec else self.metric


def _positive_float(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 须为数字")
    number = float(value)
    if number <= 0:
        raise ValueError(f"{key} 须为正数")
    return number


def _coerce_latency(params: Mapping[str, object]) -> dict:
    return {"max_latency": _positive_float(params["max_latency"], "max_latency")}


def _coerce_country(params: Mapping[str, object]) -> dict:
    allow = params.get("allow")
    if allow is None:  # 缺省不限制
        return {"allow": []}
    if not isinstance(allow, list) or not all(isinstance(c, str) for c in allow):
        raise ValueError("allow 须为国家代码字符串数组")
    return {"allow": [c.strip().upper() for c in allow if c.strip()]}


def _coerce_purity(params: Mapping[str, object]) -> dict:
    score = params["max_score"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("max_score 须为 0-100 整数")
    return {"max_score": score}


def _coerce_speed(params: Mapping[str, object]) -> dict:
    return {"min_download_mbps": _positive_float(params["min_download_mbps"], "min_download_mbps")}


def _judge_latency(params: Mapping[str, object], value: object) -> Verdict:
    limit = params["max_latency"]
    if value is None:
        return Verdict(True, "延时未测到，放过", measured=False)
    passed = value <= limit
    return Verdict(passed, f"{value:.2f}s {'≤' if passed else '>'} {limit:.2f}s")


def _judge_country(params: Mapping[str, object], value: object) -> Verdict:
    if value is None:
        return Verdict(True, "国家未测到，放过", measured=False)
    allow = params["allow"]
    if not allow:
        return Verdict(True, f"{value}（未设白名单）")
    passed = value in allow
    return Verdict(passed, f"{value} {'∈' if passed else '∉'} {{{','.join(allow)}}}")


def _judge_purity(params: Mapping[str, object], value: object) -> Verdict:
    if value is None:
        return Verdict(True, "纯净度未测到，放过", measured=False)
    limit = params["max_score"]
    passed = value <= limit
    return Verdict(passed, f"score {value} {'≤' if passed else '>'} {limit}")


def _judge_speed(params: Mapping[str, object], value: object) -> Verdict:
    if value is None:
        return Verdict(True, "测速未测到，放过", measured=False)
    limit = params["min_download_mbps"]
    passed = value >= limit
    return Verdict(passed, f"{value:.1f}Mbps {'≥' if passed else '<'} {limit:.1f}Mbps")


METRIC_SPECS: dict[str, MetricSpec] = {
    "latency": MetricSpec("延时", frozenset({"max_latency"}), frozenset(), _coerce_latency, _judge_latency),
    "country": MetricSpec("国家", frozenset(), frozenset({"allow"}), _coerce_country, _judge_country),
    "purity": MetricSpec("纯净度", frozenset({"max_score"}), frozenset(), _coerce_purity, _judge_purity),
    "speed": MetricSpec("测速", frozenset({"min_download_mbps"}), frozenset(),
                        _coerce_speed, _judge_speed),
}


def order_rules(rules: Iterable[EnqueueRule]) -> tuple[EnqueueRule, ...]:
    """按 priority 升序（priority 唯一性由配置层保证）"""
    return tuple(sorted(rules, key=lambda rule: rule.priority))


def judge(rule: EnqueueRule, value: object) -> Verdict:
    """用规则所属指标的判定函数比较测量值与阈值"""
    return METRIC_SPECS[rule.metric].judge(rule.params, value)
