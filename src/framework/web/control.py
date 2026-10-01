"""serve 运行时可变参数的集中持有（Web 面板可调，serve 主循环每轮读取最新值）

Web 控制面板与 serve 主循环共享同一个实例：面板通过 /api/settings 写入，
serve 每轮（fill / heartbeat / retry_probe）读取最新值，实现调度策略与入队筛选
规则的运行时调整，无需重启 serve 进程。

本模块属框架层，作为普通数据持有对象被 serve.py 与面板 api.py 引用，只借领域层
校验（入队规则参数用 METRIC_SPECS 校验），落地由 api 调用 standby 的方法完成。

调度算法取值（pproxy salgorithm）：fa=first_available, rr=轮询, rc=随机, lc=最少连接。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.domain.enqueue_filter import METRIC_SPECS, EnqueueRule
from src.domain.models import SUPPORTED_SCHEMES

SCHEDULE_ALGORITHMS = ("fa", "rr", "rc", "lc")


class EnqueueRuleSettings:
    """入队筛选规则（EnqueueRule）的运行时设置：启停与阈值覆盖（优先级来自配置、只读）

    rules 以指标为键（配置层保证指标唯一），保留未启用项供面板展示；
    阈值校验复用 domain.METRIC_SPECS，非法值 ValueError（API 层转 400）。
    """

    def __init__(self, rules: Iterable[EnqueueRule]) -> None:
        self._rules: dict[str, EnqueueRule] = {rule.metric: rule for rule in rules}

    @property
    def rules(self) -> tuple[EnqueueRule, ...]:
        """全部规则（含未启用），按配置声明顺序"""
        return tuple(self._rules.values())

    def to_dict(self) -> list[dict]:
        """导出为可序列化快照（/api/status 与 /api/settings 响应）"""
        return [
            {"metric": rule.metric, "label": rule.label, "priority": rule.priority,
             "enabled": rule.enabled, "params": dict(rule.params)}
            for rule in self._rules.values()
        ]

    def validate(self, patch: object) -> dict[str, EnqueueRule]:
        """纯校验：按指标解析补丁，返回候选规则表（不写内部状态）

        patch 为列表 [{metric, enabled?, ...阈值}]；只处理出现的指标，未出现的保持现状。
        任一项非法即 ValueError（API 层转 400）——校验与落地分离，失败时调用方零副作用。
        """
        if not isinstance(patch, list):
            raise ValueError("enqueue_rules 须为列表")
        rules = dict(self._rules)
        for item in patch:
            if not isinstance(item, dict):
                raise ValueError("enqueue_rules 每项须为对象")
            metric = str(item.get("metric", ""))
            rule = rules.get(metric)
            if rule is None:
                raise ValueError(f"未知入队筛选指标 {metric!r}")
            spec = METRIC_SPECS[metric]
            unknown = set(item) - {"metric", "enabled"} - spec.required - spec.optional
            if unknown:
                raise ValueError(f"{rule.label} 含未知参数 {sorted(unknown)}")
            enabled = item.get("enabled", rule.enabled)
            if not isinstance(enabled, bool):
                raise ValueError(f"{rule.label} enabled 须为布尔值")
            params = dict(rule.params)
            params.update({k: item[k] for k in (spec.required | spec.optional) if k in item})
            rules[metric] = EnqueueRule(metric=metric, priority=rule.priority,
                                        params=spec.coerce(params), enabled=enabled)
        return rules

    def apply(self, rules: Mapping[str, EnqueueRule]) -> tuple[EnqueueRule, ...]:
        """提交 validate 产出的候选规则表，返回全部规则（含未启用）"""
        self._rules = dict(rules)
        return self.rules


class ServeControl:
    """serve 运行时可变调度参数

    allowed_types 为可变 set（面板可增删允许的代理类型）；
    其余字段由 update() 校验后写入，非法值抛 ValueError（API 层转为 400）。
    """

    def __init__(
        self,
        *,
        interval: int,
        concurrency: int,
        timeout: float,
        probe_url: str,
        allowed_types: set[str],
        fill_probe_multiplier: int,
        fill_probe_min_extra: int,
        salgorithm: str = "rc",
    ) -> None:
        self.interval = interval
        self.concurrency = concurrency
        self.timeout = timeout
        self.probe_url = probe_url
        self.allowed_types = set(allowed_types)
        self.fill_probe_multiplier = fill_probe_multiplier
        self.fill_probe_min_extra = fill_probe_min_extra
        self.salgorithm = salgorithm

    def to_dict(self) -> dict:
        """导出为可序列化快照（供 /api/status 展示）"""
        return {
            "interval": self.interval,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "probe_url": self.probe_url,
            "allowed_types": sorted(self.allowed_types),
            "fill_probe_multiplier": self.fill_probe_multiplier,
            "fill_probe_min_extra": self.fill_probe_min_extra,
            "salgorithm": self.salgorithm,
        }

    def update(self, patch: dict) -> dict:
        """按补丁更新调度参数，返回实际变更的键

        仅接受已知键；数值范围做基本校验（>0）。allowed_types 为增删式的
        列表替换（调用方传完整列表）。非法值抛 ValueError。
        """
        changed: dict = {}

        def _positive_int(key: str, value) -> int:
            v = int(value)
            if v <= 0:
                raise ValueError(f"{key} 必须为正整数")
            return v

        if "interval" in patch and patch["interval"] != self.interval:
            self.interval = _positive_int("interval", patch["interval"])
            changed["interval"] = self.interval
        if "concurrency" in patch and patch["concurrency"] != self.concurrency:
            self.concurrency = _positive_int("concurrency", patch["concurrency"])
            changed["concurrency"] = self.concurrency
        if "timeout" in patch and patch["timeout"] != self.timeout:
            v = float(patch["timeout"])
            if v <= 0:
                raise ValueError("timeout 必须为正数")
            self.timeout = v
            changed["timeout"] = self.timeout
        if "probe_url" in patch and patch["probe_url"] != self.probe_url:
            url = str(patch["probe_url"]).strip()
            if not url:
                raise ValueError("probe_url 不能为空")
            self.probe_url = url
            changed["probe_url"] = self.probe_url
        if "allowed_types" in patch and sorted(patch["allowed_types"]) != sorted(self.allowed_types):
            types = set(patch["allowed_types"])
            invalid = types - set(SUPPORTED_SCHEMES)
            if invalid:
                raise ValueError(f"非法代理类型: {sorted(invalid)}")
            self.allowed_types = types
            changed["allowed_types"] = sorted(types)
        if ("fill_probe_multiplier" in patch
                and patch["fill_probe_multiplier"] != self.fill_probe_multiplier):
            self.fill_probe_multiplier = _positive_int("fill_probe_multiplier",
                                                       patch["fill_probe_multiplier"])
            changed["fill_probe_multiplier"] = self.fill_probe_multiplier
        if ("fill_probe_min_extra" in patch
                and patch["fill_probe_min_extra"] != self.fill_probe_min_extra):
            self.fill_probe_min_extra = _positive_int("fill_probe_min_extra",
                                                      patch["fill_probe_min_extra"])
            changed["fill_probe_min_extra"] = self.fill_probe_min_extra
        if "salgorithm" in patch and patch["salgorithm"] != self.salgorithm:
            alg = str(patch["salgorithm"])
            if alg not in SCHEDULE_ALGORITHMS:
                raise ValueError(f"非法调度算法 {alg!r}: 可选 {SCHEDULE_ALGORITHMS}")
            self.salgorithm = alg
            changed["salgorithm"] = alg
        return changed
