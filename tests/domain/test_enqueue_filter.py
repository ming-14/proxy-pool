"""入队筛选规则领域单测：优先级排序、参数校验、判定边界"""

import unittest

from src.domain.enqueue_filter import METRIC_SPECS, EnqueueRule, judge, order_rules


DEFAULT_PARAMS = {
    "latency": {"max_latency": 1.0},
    "country": {},
    "purity": {"max_score": 50},
    "speed": {"min_download_mbps": 10},
}


def make_rule(metric: str, priority: int, params: dict | None = None,
              enabled: bool = True) -> EnqueueRule:
    """按指标校验规则参数（与配置层同一条路径）"""
    merged = {**DEFAULT_PARAMS[metric], **(params or {})}
    return EnqueueRule(metric=metric, priority=priority,
                       params=METRIC_SPECS[metric].coerce(merged), enabled=enabled)


class TestOrderRules(unittest.TestCase):
    def test_orders_by_priority_ascending(self):
        rules = [make_rule("speed", 4), make_rule("latency", 1), make_rule("purity", 3)]
        self.assertEqual([r.metric for r in order_rules(rules)], ["latency", "purity", "speed"])

    def test_stable_for_equal_priority(self):
        """同优先级保持输入顺序（配置层禁止重复优先级，此处只锁排序稳定性）"""
        rules = [make_rule("country", 1), make_rule("latency", 1)]
        self.assertEqual([r.metric for r in order_rules(rules)], ["country", "latency"])

    def test_label_comes_from_metric_spec(self):
        self.assertEqual(make_rule("latency", 1, {"max_latency": 1.0}).label, "延时")


class TestCoerce(unittest.TestCase):
    """参数校验发生在配置加载期（错误必须启动即暴露，而不是运行期）"""

    def test_latency_requires_positive_number(self):
        self.assertEqual(make_rule("latency", 1, {"max_latency": 2}).params["max_latency"], 2.0)
        for bad in (0, -1, "fast", True):
            with self.assertRaises(ValueError):
                make_rule("latency", 1, {"max_latency": bad})

    def test_country_allow_optional_and_normalized(self):
        self.assertEqual(make_rule("country", 2).params["allow"], [])
        self.assertEqual(make_rule("country", 2, {"allow": ["us", " jp "]}).params["allow"], ["US", "JP"])

    def test_country_allow_must_be_string_list(self):
        for bad in ("US", [1, 2], {}):
            with self.assertRaises(ValueError):
                make_rule("country", 2, {"allow": bad})

    def test_purity_score_range(self):
        self.assertEqual(make_rule("purity", 3, {"max_score": 0}).params["max_score"], 0)
        for bad in (-1, 101, 50.5, True):
            with self.assertRaises(ValueError):
                make_rule("purity", 3, {"max_score": bad})

    def test_speed_requires_positive_mbps(self):
        self.assertEqual(make_rule("speed", 4, {"min_download_mbps": 10}).params["min_download_mbps"], 10.0)
        with self.assertRaises(ValueError):
            make_rule("speed", 4, {"min_download_mbps": 0})


class TestJudges(unittest.TestCase):
    def test_latency_boundary_inclusive(self):
        rule = make_rule("latency", 1, {"max_latency": 1.0})
        self.assertTrue(judge(rule, 1.0).passed)
        self.assertFalse(judge(rule, 1.01).passed)

    def test_country_allowlist_and_empty_means_no_limit(self):
        rule = make_rule("country", 2, {"allow": ["US", "JP"]})
        self.assertTrue(judge(rule, "US").passed)
        self.assertFalse(judge(rule, "RU").passed)
        self.assertTrue(judge(make_rule("country", 2), "RU").passed)

    def test_purity_lower_is_cleaner(self):
        rule = make_rule("purity", 3, {"max_score": 50})
        self.assertTrue(judge(rule, 50).passed)
        self.assertFalse(judge(rule, 51).passed)

    def test_speed_higher_is_better(self):
        rule = make_rule("speed", 4, {"min_download_mbps": 10})
        self.assertTrue(judge(rule, 10.0).passed)
        self.assertFalse(judge(rule, 9.9).passed)

    def test_missing_value_passes_and_is_marked_unmeasured(self):
        """缺数据放过：不该因我方查不到而拒绝一个可用代理"""
        for metric, params in (("latency", {"max_latency": 0.1}), ("country", {"allow": ["US"]}),
                               ("purity", {"max_score": 0}), ("speed", {"min_download_mbps": 9999})):
            verdict = judge(make_rule(metric, 1, params), None)
            self.assertTrue(verdict.passed, metric)
            self.assertFalse(verdict.measured, metric)
            self.assertIn("未测到", verdict.detail)
