"""入队筛选用例：按优先级惰性测量并短路判定

候选须通过全部启用规则才允许入队。测量是惰性的——某条规则未通过时，后续指标的
测量（可能含外网请求与带宽代价）完全不发生。

测量源由组合根按指标注册（latency 直接取 fill 探测结果，不占 provider）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from src.domain.enqueue_filter import EnqueueRule, Verdict, judge, order_rules

logger = logging.getLogger(__name__)

# 指标测量源：proxy -> 测量值（None = 测不到，判定时按放过处理）
MetricProvider = Callable[[str], Awaitable[object | None]]


@dataclass(frozen=True)
class FilterOutcome:
    """一次筛选结果；trace 为实际执行的判定路径"""
    passed: bool
    rejected_by: EnqueueRule | None
    trace: tuple[tuple[EnqueueRule, Verdict], ...]

    def workflow(self) -> str:
        """可读判定路径（被短路而未执行的规则不出现在 trace 里）"""
        text = " → ".join(
            f"{rule.label}[{rule.priority}] {verdict.detail}" for rule, verdict in self.trace
        )
        return text if self.passed else f"{text} → 跳过后续"


class EnqueueReadings:
    """单个候选的惰性测量缓存：走到哪个指标才测哪个，同指标只测一次"""

    def __init__(self, proxy: str, *, latency: float | None,
                 providers: Mapping[str, MetricProvider]) -> None:
        self._proxy = proxy
        self._latency = latency
        self._providers = providers
        self._cache: dict[str, object | None] = {}

    async def get(self, metric: str) -> object | None:
        if metric in self._cache:
            return self._cache[metric]
        if metric == "latency":
            value: object | None = self._latency
        else:
            provider = self._providers.get(metric)
            value = await provider(self._proxy) if provider else None
        self._cache[metric] = value
        return value


class EnqueueFilterUseCase:
    """入队筛选：按 priority 升序逐条判定，未通过即短路"""

    def __init__(self, *, rules: Iterable[EnqueueRule],
                 providers: Mapping[str, MetricProvider] | None = None) -> None:
        self._providers: Mapping[str, MetricProvider] = dict(providers or {})
        self._rules: tuple[EnqueueRule, ...] = ()
        self.update_rules(rules)

    def update_rules(self, rules: Iterable[EnqueueRule]) -> None:
        """替换规则集合并重排序（网页设置开启/关闭/改阈值时调用）"""
        self._rules = order_rules(rule for rule in rules if rule.enabled)
        for rule in self._rules:
            if rule.metric != "latency" and rule.metric not in self._providers:
                logger.warning("入队筛选规则 %s 无可用测量源，将始终放过", rule.label)

    @property
    def rules(self) -> tuple[EnqueueRule, ...]:
        """当前启用的规则（已按优先级升序）"""
        return self._rules

    async def evaluate(self, proxy: str, *, latency: float | None) -> FilterOutcome:
        """判定单个候选

        本方法不外抛：异常若逃到调度器会被当作该代理探测失败，把筛选故障算到
        代理头上是错的。
        """
        readings = EnqueueReadings(proxy, latency=latency, providers=self._providers)
        trace: list[tuple[EnqueueRule, Verdict]] = []
        rejected_by = None
        try:
            for rule in self._rules:
                verdict = judge(rule, await readings.get(rule.metric))
                trace.append((rule, verdict))
                if not verdict.passed:
                    rejected_by = rule
                    break
        except Exception:
            logger.exception("入队筛选异常，候选 %s 放行", proxy)
            return FilterOutcome(True, None, tuple(trace))
        outcome = FilterOutcome(rejected_by is None, rejected_by, tuple(trace))
        logger.debug("入队筛选 %s: %s", proxy, outcome.workflow())
        return outcome
