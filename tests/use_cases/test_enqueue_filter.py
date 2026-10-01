"""入队筛选用例单测：惰性测量、短路顺序、缺源与故障处理"""

import unittest

from src.domain.enqueue_filter import METRIC_SPECS, EnqueueRule
from src.use_cases.enqueue_filter import EnqueueFilterUseCase


def make_rule(metric: str, priority: int, params: dict | None = None,
              enabled: bool = True) -> EnqueueRule:
    return EnqueueRule(metric=metric, priority=priority,
                       params=METRIC_SPECS[metric].coerce(params or {}), enabled=enabled)


def make_provider(value, calls: list[str], name: str):
    """记录被调用的指标测量源"""
    async def provider(_proxy: str):
        calls.append(name)
        return value
    return provider


class TestEnqueueFilter(unittest.TestCase):
    def test_no_enabled_rules_passes_without_measuring(self):
        calls: list[str] = []
        flt = EnqueueFilterUseCase(
            rules=[make_rule("country", 2, {"allow": ["US"]}, enabled=False)],
            providers={"country": make_provider("RU", calls, "country")},
        )
        outcome = self._run(flt, 0.5)
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.trace, ())
        self.assertEqual(calls, [])

    def test_short_circuit_skips_downstream_measurement(self):
        """核心保证：首条规则未通过时，后续指标一次都不测（外网/带宽代价不发生）"""
        calls: list[str] = []
        flt = EnqueueFilterUseCase(
            rules=[make_rule("latency", 1, {"max_latency": 1.0}),
                   make_rule("country", 2, {"allow": ["US"]}),
                   make_rule("speed", 4, {"min_download_mbps": 10})],
            providers={"country": make_provider("US", calls, "country"),
                       "speed": make_provider(100.0, calls, "speed")},
        )
        outcome = self._run(flt, 5.0)          # 延时 5s > 1s
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.rejected_by.metric, "latency")
        self.assertEqual(calls, [])             # 国家/测速均未执行
        self.assertNotIn("测速", outcome.workflow())

    def test_runs_in_priority_order_and_reports_full_trace(self):
        calls: list[str] = []
        flt = EnqueueFilterUseCase(
            rules=[make_rule("speed", 4, {"min_download_mbps": 10}),
                   make_rule("country", 2, {"allow": ["US"]})],
            providers={"country": make_provider("US", calls, "country"),
                       "speed": make_provider(50.0, calls, "speed")},
        )
        outcome = self._run(flt, 0.2)
        self.assertTrue(outcome.passed)
        self.assertEqual(calls, ["country", "speed"])   # priority 升序
        self.assertEqual([rule.metric for rule, _ in outcome.trace], ["country", "speed"])

    def test_metric_without_provider_passes(self):
        """未接入测量源的指标按放过处理（例如规则先行开启、provider 后置）"""
        flt = EnqueueFilterUseCase(rules=[make_rule("purity", 3, {"max_score": 0})])
        outcome = self._run(flt, 0.1)
        self.assertTrue(outcome.passed)
        self.assertFalse(outcome.trace[0][1].measured)

    def test_each_metric_measured_once(self):
        calls: list[str] = []
        flt = EnqueueFilterUseCase(
            rules=[make_rule("country", 1, {"allow": ["US"]}),
                   make_rule("country", 2, {"allow": ["US"]})],
            providers={"country": make_provider("US", calls, "country")},
        )
        self.assertTrue(self._run(flt, 0.1).passed)
        self.assertEqual(calls, ["country"])

    def test_provider_failure_does_not_reject_candidate(self):
        """筛选自身故障不得算到代理头上：放行并告警"""
        async def boom(_proxy: str):
            raise RuntimeError("provider 挂了")

        flt = EnqueueFilterUseCase(rules=[make_rule("country", 1, {"allow": ["US"]})],
                                   providers={"country": boom})
        self.assertTrue(self._run(flt, None).passed)

    def test_rule_update_takes_effect(self):
        flt = EnqueueFilterUseCase(rules=[make_rule("latency", 1, {"max_latency": 1.0}, enabled=False)])
        self.assertTrue(self._run(flt, 9.0).passed)
        flt.update_rules([make_rule("latency", 1, {"max_latency": 1.0})])
        self.assertFalse(self._run(flt, 9.0).passed)

    @staticmethod
    def _run(flt: EnqueueFilterUseCase, latency: float | None):
        import asyncio
        return asyncio.run(flt.evaluate("http://1.2.3.4:8080", latency=latency))
