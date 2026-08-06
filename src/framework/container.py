"""组合根：组装领域池、存储、用例，并向 CLI 暴露依赖

依赖方向：framework 层 import 所有内层（interfaces/use_cases/domain），
内层不 import 本模块。
"""

import importlib
from dataclasses import dataclass

from src.domain.models import SourceSpec
from src.domain.ports import ProxySource
from src.domain.proxy_pool import ProxyPool
from src.interfaces.http_client import HttpGetter
from src.interfaces.json_store import JsonProxyStore
from src.interfaces.proxy_server import PproxyServer
from src.interfaces.scheduler import ProxyScheduler
from src.use_cases.check_proxies import CheckProxiesUseCase
from src.use_cases.fetch_proxies import FetchProxiesUseCase
from src.use_cases.get_proxies import GetProxiesUseCase
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.request_with_retry import RequestWithRetryUseCase
from src.use_cases.standby import StandbyQueueUseCase


@dataclass
class Container:
    """已组装依赖的容器，CLI 命令从中取用各组件"""

    pool: ProxyPool
    store: JsonProxyStore
    fetch: FetchProxiesUseCase
    get_proxies: GetProxiesUseCase
    record: RecordProxyUseCase
    request_with_retry: RequestWithRetryUseCase
    check_proxies: CheckProxiesUseCase
    proxy_server: PproxyServer
    standby: StandbyQueueUseCase

    def save(self) -> None:
        """将池状态持久化（CLI 命令 finally 中调用）"""
        self.store.save(self.pool.export_state())


def _source_loader(name: str) -> ProxySource:
    """按源名加载源实例（源模块暴露 create_source 工厂函数）"""
    return importlib.import_module(name).create_source()


def build_container(cache_file: str | None = None) -> Container:
    """构建完整依赖树。cache_file 缺省时使用 config.CACHE_FILE"""
    from src.framework import config

    pool = ProxyPool(
        config.PROXY_GROUPS,
        max_fails=config.MAX_FAILS,
        trusted_max_fails=config.TRUSTED_MAX_FAILS,
        default_pause_ttl=config.SOURCE_PAUSE_TTL,
        requestable_groups=config.REQUESTABLE_GROUPS,
        trusted_group=config.TRUSTED_GROUP,
        blacklist_group=config.BLACKLIST_GROUP,
    )
    store = JsonProxyStore(cache_file or config.CACHE_FILE)
    pool.restore_state(store.load())

    sources = [SourceSpec(**s) for s in config.SOURCES]
    http_getter = HttpGetter()

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
    )
    proxy_server = PproxyServer(
        listen_uri=f"http+socks4+socks5://{config.SERVER_HOST}:{config.SERVER_PORT}",
    )
    standby = StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check_proxies,
        standby_group=config.STANDBY_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
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
    )
