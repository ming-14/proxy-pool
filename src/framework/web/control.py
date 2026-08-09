"""serve 运行时调度参数的集中持有（Web 面板可调，serve 主循环每轮读取最新值）

Web 控制面板与 serve 主循环共享同一个 ServeControl 实例：面板通过 /api/settings
写入，serve 每轮（fill / heartbeat / retry_probe）读取最新值，实现调度策略的
运行时调整，无需重启 serve 进程。

本模块属框架层，作为普通数据持有对象被 serve.py 与面板 api.py 引用，不依赖
领域/用例层具体实现（standby 填充倍率的落地由 api 调用
standby.update_fill_probe_params 完成）。

调度算法取值（pproxy salgorithm）：fa=first_available, rr=轮询, rc=随机, lc=最少连接。
"""

from __future__ import annotations

SCHEDULE_ALGORITHMS = ("fa", "rr", "rc", "lc")


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
            invalid = types - {"http", "https", "socks5"}
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
