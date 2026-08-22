"""领域层抽象端口（接口适配器层实现它们）

依赖方向：use_cases / interfaces 依赖这里的 Protocol，实现方无需继承，
只需满足结构签名。领域层本身零第三方依赖、无 IO。
"""

from __future__ import annotations

from typing import Any, Protocol


class ProxySource(Protocol):
    """代理源端口：拉取代理列表（ip:port 字符串）"""

    def fetch_proxies(
        self, retries: int | None = None, timeout: float | None = None
    ) -> list[str]: ...


class ProxyStore(Protocol):
    """代理池状态存储端口：支持增量与全量两种持久化模式

    增量（save_group/save_source_paused）只写 dirty 组，心跳高频场景 ~0.3ms；
    全量（save_full）替换整库，CLI/serve 退出时确保一致性。
    领域规则（过期过滤）在 ProxyPool，store 只做读写。
    """

    def load(self) -> dict:
        """加载全量状态（启动时调用，返回 export_state 兼容格式）"""
        ...

    def save_full(self, state: dict) -> None:
        """全量替换（CLI 命令退出 / serve 退出，确保一致性）"""
        ...

    def save_group(self, group_name: str, group_data: dict) -> None:
        """增量保存单个组（事务内 DELETE 该组旧行 + INSERT 新行）

        Args:
            group_data: {"ttl": ..., "proxies": [{"proxy", "joined_at", ...}]}
        """
        ...

    def save_source_paused(self, source_paused: dict) -> None:
        """增量保存源暂停状态（全量替换 source_paused 表）"""
        ...


class HttpGetter(Protocol):
    """HTTP 请求端口：支持通过代理发起 GET；失败抛异常

    返回 duck-typed response（只读取 status_code/text/headers/encoding 属性），
    用例层不 import requests。
    verify: 是否校验证书。默认 False（免费代理多自签证书，直连校验会误杀）；
            探测类用例（check_proxies）对 http/socks5 代理显式传 True 以筛掉伪造证书的
            中间人代理（隧道端到端校验目标证书）；https 代理 TLS 直连代理本身，
            校验的是代理自签证书，仍传 False（见 check_proxies.do_test）。
    """

    def get(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict | None = None,
        proxies: dict | None = None,
        verify: bool = False,
    ) -> Any: ...


class TaskScheduler(Protocol):
    """并发任务调度器端口（CheckProxiesUseCase 只用到 run）"""

    async def run(
        self,
        tasks: list,
        phase_name: str,
        do_task: Any,
        on_success: Any = None,
    ) -> dict: ...


class IpPurityChecker(Protocol):
    """IP 纯洁度检查端口：查询 IP 的滥用举报信息（AbuseIPDB 等）

    返回 dict 含 abuse_confidence_score(0-100) / usage_type / country_code /
    isp / total_reports 等字段。实现方直连外部 API，不走代理。
    """

    def check(self, ip: str, *, max_age_in_days: int = 90) -> dict: ...


class PurityCacheStore(Protocol):
    """IP 纯洁度缓存存储端口：持久化检查结果避免重复调 API（AbuseIPDB 有日限额）

    TTL 判断在用例层（缓存策略非领域规则）；store 只做纯读写。
    """

    def get_purity(self, ip: str) -> dict | None:
        """读取缓存记录；返回 {"result": {...}, "cached_at": float}，None 表示无缓存"""
        ...

    def save_purity(self, ip: str, result: dict) -> None:
        """写入/更新缓存（upsert by ip）"""
        ...
