"""组合根：组装领域池、存储、用例，并向 CLI 暴露依赖

依赖方向：framework 层 import 所有内层（interfaces/use_cases/domain），
内层不 import 本模块。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

from src.domain.models import SourceSpec
from src.domain.ports import ProxySource, IpPurityChecker
from src.domain.proxy_pool import ProxyPool
from src.interfaces.abuseipdb_client import AbuseIpDbClient
from src.interfaces.geoip_client import GeoLite2CityLocator
from src.interfaces.http_client import HttpGetter, AsyncHttpGetter
from src.interfaces.ipok_client import IpokClient
from src.interfaces.sqlite_store import SqliteProxyStore
from src.interfaces.proxy_server import PproxyServer
from src.interfaces.scheduler import ProxyScheduler
from src.interfaces.sources.freeproxy_world import FpwConfig
from src.use_cases.autoload import AutoloadUseCase
from src.use_cases.check_proxies import CheckProxiesUseCase
from src.use_cases.check_purity import CheckPurityUseCase
from src.use_cases.fetch_proxies import FetchProxiesUseCase
from src.use_cases.get_proxies import GetProxiesUseCase
from src.use_cases.ip_geo import IpGeoUseCase
from src.use_cases.network_gate import LocalNetworkGate
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.request_with_retry import RequestWithRetryUseCase
from src.use_cases.standby import StandbyQueueUseCase


@dataclass
class Container:
    """已组装依赖的容器，CLI 命令从中取用各组件"""

    pool: ProxyPool
    store: SqliteProxyStore
    fetch: FetchProxiesUseCase
    get_proxies: GetProxiesUseCase
    record: RecordProxyUseCase
    request_with_retry: RequestWithRetryUseCase
    check_proxies: CheckProxiesUseCase
    proxy_server: PproxyServer
    standby: StandbyQueueUseCase
    async_getter: AsyncHttpGetter
    purity: CheckPurityUseCase          # IP 纯洁度检查（多源聚合：AbuseIPDB + ipok.io）
    geo: IpGeoUseCase                   # IP 归属地图（GeoLite2 定位 + 缓存 + 聚合）

    def save(self) -> None:
        """增量保存：只写 dirty 组（serve 主循环 / Web 控制动作高频调用）

        无 dirty 时零 IO；心跳场景 ~0.3ms（vs 旧 JSON 全量 0.3s）。
        """
        dirty = self.pool.dirty_groups()
        if dirty:
            for g in dirty:
                self.store.save_group(g, self.pool.export_group(g))
        if self.pool.source_paused_dirty():
            self.store.save_source_paused(self.pool.export_source_paused())
        self.pool.clear_dirty()

    def save_full(self) -> None:
        """全量保存（CLI 退出 / serve 退出，确保一致性）"""
        self.store.save_full(self.pool.export_state())
        self.pool.clear_dirty()


def _source_loader(name: str) -> ProxySource:
    """按源名加载源实例（源模块暴露 create_source 工厂函数，FPW 配置经组合根注入）

    create_source 统一签名为 create_source(config)，无专属参数的源忽略 config。
    """
    from src.framework import config

    fpw_config = FpwConfig(
        accept_language=config.FPW_ACCEPT_LANGUAGE,
        max_concurrent=config.FPW_MAX_CONCURRENT,
        proxy_retry=config.FPW_PROXY_RETRY,
        request_timeout=config.FPW_REQUEST_TIMEOUT,
        task_interval=config.FPW_TASK_INTERVAL,
    )
    return importlib.import_module(name).create_source(config=fpw_config)


def _load_autoload(pool: ProxyPool, standby_group: str, file_path: str) -> None:
    """启动时从 autoload 清单加载代理到代理服务队列（固定、跳过已存在）

    framework 层 IO：读清单文件并委托 AutoloadUseCase（纯逻辑）入池。
    文件不存在时静默跳过（autoload 为可选功能，不强制存在）。
    """
    path = Path(file_path)
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    AutoloadUseCase(pool=pool, standby_group=standby_group).execute(content)


def build_container(cache_db: str | None = None) -> Container:
    """构建完整依赖树。cache_db 缺省时使用 config.CACHE_DB"""
    from src.framework import config

    pool = ProxyPool(
        config.PROXY_GROUPS,
        max_consecutive_fails=config.MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=config.TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=config.SOURCE_PAUSE_TTL,
        requestable_groups=config.REQUESTABLE_GROUPS,
        trusted_group=config.TRUSTED_GROUP,
        blacklist_group=config.BLACKLIST_GROUP,
    )
    store = SqliteProxyStore(cache_db or config.CACHE_DB)
    pool.restore_state(store.load())
    # autoload：启动时从清单文件加载固定代理到代理服务队列（在 restore 之后，跳过已存在）
    _load_autoload(pool, config.STANDBY_GROUP, config.AUTOLOAD_FILE)

    sources = [SourceSpec(**s) for s in config.SOURCES]
    http_getter = HttpGetter()
    async_getter = AsyncHttpGetter()

    fetch = FetchProxiesUseCase(
        pool=pool,
        sources=sources,
        loader=_source_loader,
        fetch_retries=config.FETCH_PROXY_RETRIES,
        dedup_threshold=config.SOURCE_DEDUP_THRESHOLD,
    )
    get_proxies = GetProxiesUseCase(pool=pool, fetch=fetch.execute)
    record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
    request_with_retry = RequestWithRetryUseCase(
        get_proxies=get_proxies,
        record=record,
        http_getter=http_getter,
    )
    check_proxies = CheckProxiesUseCase(
        scheduler_factory=lambda proxies, **kw: ProxyScheduler(proxies, **kw),
        http_getter=http_getter,
        async_getter=async_getter,
    )
    proxy_server = PproxyServer(
        listen_uri=f"http+socks4+socks5://{config.SERVER_HOST}:{config.SERVER_PORT}",
        metrics_window=config.METRICS_WINDOW,
    )
    network_gate = LocalNetworkGate(
        http_getter=http_getter,
        probe_urls=config.NETWORK_PROBE_URLS,
        cache_ok=config.NETWORK_CHECK_CACHE_OK,
        backoff_initial=config.NETWORK_CHECK_BACKOFF_INITIAL,
        backoff_max=config.NETWORK_CHECK_BACKOFF_MAX,
        async_getter=async_getter,
    )
    standby = StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check_proxies,
        network_gate=network_gate,
        standby_group=config.STANDBY_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
        metrics_window=config.METRICS_WINDOW,
    )
    # IP 纯洁度检查：单源，由 config.PURITY_SOURCE 决定（ipok / abuseipdb）
    if config.PURITY_SOURCE == "ipok":
        purity_checker: IpPurityChecker = IpokClient(proxy=config.IPOK_PROXY)
    elif config.PURITY_SOURCE == "abuseipdb":
        if not config.ABUSEIPDB_API_KEY:
            raise ValueError("purity.source='abuseipdb' 但未配置 ABUSEIPDB_API_KEY（config/apikey.env）")
        purity_checker = AbuseIpDbClient(api_key=config.ABUSEIPDB_API_KEY, proxy=config.ABUSEIPDB_PROXY)
    else:
        raise ValueError(f"未知 purity.source: {config.PURITY_SOURCE!r}（应为 ipok 或 abuseipdb）")
    purity = CheckPurityUseCase(
        checker=purity_checker,
        cache=store,
        max_age_in_days=config.ABUSEIPDB_MAX_AGE_DAYS,
        cache_ttl=config.ABUSEIPDB_CACHE_TTL,
    )
    # IP 归属地图：GeoLite2 离线定位 + SQLite 缓存（Web 面板地图页面按需懒查）
    geo = IpGeoUseCase(
        locator=GeoLite2CityLocator(config.GEOIP_DB_FILE),
        cache=store,
        cache_ttl=config.GEOIP_CACHE_TTL,
    )
    return Container(
        pool=pool,
        store=store,
        fetch=fetch,
        get_proxies=get_proxies,
        record=record,
        request_with_retry=request_with_retry,
        check_proxies=check_proxies,
        proxy_server=proxy_server,
        standby=standby,
        async_getter=async_getter,
        purity=purity,
        geo=geo,
    )
