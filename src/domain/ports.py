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
            探测类用例（check_proxies）对 http/socks4/socks5 代理显式传 True 以筛掉伪造证书的
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


class HostResolver(Protocol):
    """域名解析端口：穿指定代理做远端 DNS 查询（返回 A 记录）

    实现方须从代理出口发出查询（如 DoH）——同一域名在不同出口的解析落点
    可能不同（CDN 就近调度），本机直连解析的结果不代表代理视角。
    失败抛异常；返回空列表表示**查询成功但无 A 记录**（调用方可据此落盘
    "已探测无结果"并缓存，避免反复重探）。
    """

    async def resolve(self, host: str, *, proxy: str) -> list[str]: ...


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
    """IP 纯净度检查端口（插件实现，见 interfaces/ippure/）

    返回 {score, usage_type, extra}：score 为 0-100 整数（越高越脏，None = 源未给出），
    usage_type 为 domain.purity.UsageType 成员（源原值由插件映射），extra 为源特有字段。
    实现方直连外部 API；代理与超时由组合根按 ippure.toml 注入，实现方不读配置。
    """

    def check(self, ip: str) -> dict: ...


class PurityCacheStore(Protocol):
    """IP 纯净度缓存存储端口：持久化检查结果避免重复调 API（部分源有日限额）

    按 (源名, IP) 分区：同一 IP 在不同源下的分数不同，换源不得复用旧源结果。
    TTL 判断在用例层（缓存策略非领域规则）；store 只做纯读写。
    """

    def get_purity(self, source: str, ip: str) -> dict | None:
        """读取缓存记录；返回 {"result": {...}, "cached_at": float}，None 表示无缓存"""
        ...

    def save_purity(self, source: str, ip: str, result: dict) -> None:
        """写入/更新缓存（upsert by (source, ip)）"""
        ...


class GeoLocator(Protocol):
    """IP 归属地定位端口：查询 IP 的国家/省市/经纬度（GeoLite2-City/Country 等离线库）

    返回 dict 含 country / country_code / province / city / lat / lng
    （Country 库无坐标，后四者可为 None，坐标兜底由用例层处理）；
    无法定位（保留地址、库中无记录）返回 None。实现方为纯本地查询，无网络 IO。
    """

    def locate(self, ip: str) -> dict | None: ...

    @property
    def fingerprint(self) -> str:
        """来源库指纹（文件名 + 构建版本），库切换/升级时用于失效 geo 缓存"""
        ...


class GeoCacheStore(Protocol):
    """IP 归属地缓存存储端口：持久化定位结果，避免重复解析 mmdb

    定位结果长期稳定（IP 段归属变化以月计），TTL 判断在用例层；store 只做纯读写。
    """

    def get_geo_batch(self, ips: list[str]) -> dict[str, dict]:
        """批量读取缓存；返回 {ip: {"country", "country_code", "province", "city", "lat", "lng", "cached_at"}}"""
        ...

    def save_geo_batch(self, results: dict[str, dict]) -> None:
        """批量写入/更新缓存（upsert by ip）"""
        ...

    def get_geo_cache_fingerprint(self) -> str | None:
        """读缓存来源库指纹；未记录返回 None"""
        ...

    def set_geo_cache_fingerprint(self, fingerprint: str) -> None:
        """记录缓存来源库指纹"""
        ...

    def clear_geo_cache(self) -> None:
        """清空归属缓存（库切换/升级时调用）"""
        ...


class DnsProbeStore(Protocol):
    """(代理, 目标域名) DNS 探测结果缓存存储端口：TTL/清理阈值判断在用例层，store 只做纯读写

    "已探测无结果"行（ip/geo 全空）照常保存，TTL 内不重复探测；消费方只使用
    lat/lng 非空的行画点。
    """

    def get_dns_probe_batch(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """批量读取；返回 {(bind, host): {ip, country, country_code, province, city, lat, lng, cached_at}}"""
        ...

    def save_dns_probe_batch(self, rows: dict[tuple[str, str], dict]) -> None:
        """批量写入/更新（upsert by (bind, host)）"""
        ...

    def prune_dns_probe(self, before_ts: float) -> int:
        """删除 cached_at 早于 before_ts 的行，返回删除数"""
        ...


class TrafficHistoryStore(Protocol):
    """历史流量条目存储端口（按 (代理,域名,IP) 分区）：保留期判断在用例层，store 只做纯读写

    写入是**增量累加**语义（见实现），不是绝对值覆盖——进程重启后内存增量归零，
    写绝对值会把已落盘的历史冲掉。
    """

    def add_traffic_history(self, deltas: dict[tuple[str, str, str], dict]) -> None:
        """按增量累加写入；deltas: {(bind, host, ip): {up, down, last_seen}}"""
        ...

    def get_traffic_history(self, limit: int) -> list[dict]:
        """按流量总计降序读取前 limit 条"""
        ...

    def count_traffic_history(self) -> int:
        """条目总数（不受 limit 截断影响）"""
        ...

    def prune_traffic_history(self, before_ts: float) -> int:
        """删除 last_seen 早于 before_ts 的行，返回删除数"""
        ...


class ProxySpeedTester(Protocol):
    """代理测速端口：测 Cloudflare 下载速率（Mbps）

    measure 返回面板展示的完整结果（download/upload 精简 dict）；download_mbps 是入队筛选的
    轻量入口，带缓存与串行保护。两者均为同步阻塞 IO（调用方放 executor，不阻塞事件循环）；
    测不出（超时/代理不可用/无速率）返回 None，判定层按未测到放过。
    """

    def measure(self, proxy: str) -> dict:
        """完整测速结果：{proxy, download, upload}"""
        ...

    def download_mbps(self, proxy: str) -> float | None: ...
