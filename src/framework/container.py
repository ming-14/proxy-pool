"""组合根：组装领域池、存储、用例，并向 CLI 暴露依赖

依赖方向：framework 层 import 所有内层（interfaces/use_cases/domain），
内层不 import 本模块。

重依赖惰性导入：纯净度检查插件（requests 链）与 GeoLite2 定位器（geoip2 链）
仅在对应功能启用的分支内 import——serve 稳态（心跳/门控走 aiohttp）与
geoip.enabled=false 场景不加载这些依赖树（边际 ~5.6MB，调研见会话记录）。
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from src.domain.models import SourceSpec, FpwConfig
from src.domain.ports import ProxySource
from src.domain.purity import IppurePluginConfig, IppureSourceSpec, resolve_proxy
from src.domain.proxy_pool import ProxyPool
from src.interfaces.http_client import HttpGetter, AsyncHttpGetter
from src.interfaces.sqlite_store import SqliteProxyStore
from src.interfaces.proxy_server import PproxyServer
from src.interfaces.scheduler import ProxyScheduler
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
    purity: CheckPurityUseCase | None   # IP 纯净度检查；web 关闭/未启用源/缺密钥时为 None（功能禁用）
    geo: IpGeoUseCase | None          # IP 归属地图（GeoLite2 定位 + 缓存 + 聚合）；geoip.enabled=false 时为 None

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


def _load_ippure(store: SqliteProxyStore) -> CheckPurityUseCase | None:
    """加载纯净度检查插件并组装用例（仅面板消费）

    配置错误（插件模块导入失败/缺 SOURCE/插件名重复/节未被声明/未知源名/插件参数非法）
    直接抛错——启动即暴露，不做运行期兜底；选中源缺必需环境变量时只警告并禁用，
    不阻断启动。插件模块仅在本函数内 import，保持依赖链惰性。
    """
    from src.framework import config

    if not config.WEB_ENABLED or not config.IPPURE_SOURCE:
        return None
    registry: dict[str, tuple[object, IppureSourceSpec]] = {}
    for module_path in config.IPPURE_PLUGIN_MODULES:
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ValueError(f"ippure 插件模块导入失败: {module_path}（{exc}）") from exc
        spec = getattr(module, "SOURCE", None)
        if not isinstance(spec, IppureSourceSpec):
            raise ValueError(f"{module_path} 未暴露 IppureSourceSpec 类型的模块级 SOURCE")
        factory = getattr(module, "create_checker", None)
        if not callable(factory):
            raise ValueError(f"{module_path} 未暴露 create_checker 工厂函数")
        if spec.name in registry:
            raise ValueError(f"ippure 插件名重复: {spec.name!r}（{module_path}）")
        registry[spec.name] = (factory, spec)
    undeclared = set(config.IPPURE_SECTIONS) - set(registry)
    if undeclared:
        raise ValueError(
            f"ippure.toml 含未被任何插件声明的节 {sorted(undeclared)}"
            f"（已声明的插件名 {sorted(registry)}）"
        )
    entry = registry.get(config.IPPURE_SOURCE)
    if entry is None:
        raise ValueError(
            f"未知 ippure.source={config.IPPURE_SOURCE!r}（可选 {sorted(registry)}）"
        )
    factory, spec = entry
    missing = [key for key in spec.env_keys if not os.environ.get(key)]
    if missing:
        logger.warning("纯净度检查已禁用：缺少环境变量 %s（见 config/.env）", ", ".join(missing))
        return None
    proxy = resolve_proxy(config.IPPURE_PROXY_RULES, spec.name)
    label = spec.label or spec.name  # 插件未声明展示名时退回插件名，避免面板出现空来源
    logger.info("纯净度检查源 %s(%s) 经 %s", spec.name, label, proxy or "直连")
    checker = factory(IppurePluginConfig(
        timeout=config.IPPURE_TIMEOUT,
        proxy=proxy,
        section=config.IPPURE_SECTIONS.get(spec.name, {}),
        env={key: os.environ[key] for key in spec.env_keys},
    ))
    return CheckPurityUseCase(
        checker,
        source=spec.name,
        label=label,
        cache_ttl=config.IPPURE_CACHE_TTL,
        cache=store,
    )


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
        async_getter=async_getter,
    )
    proxy_server = PproxyServer(
        listen_uri=f"http+socks4+socks5://{config.SERVER_HOST}:{config.SERVER_PORT}",
        metrics_window=config.METRICS_WINDOW,
    )
    network_gate = LocalNetworkGate(
        async_getter=async_getter,
        probe_urls=config.NETWORK_PROBE_URLS,
        probe_timeout=config.NETWORK_PROBE_TIMEOUT,
        unhealthy_cache_ttl=config.NETWORK_UNHEALTHY_CACHE_TTL,
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
        chart_window=config.STANDBY_CHART_WINDOW,
    )
    # IP 纯净度 / IP 归属：仅 Web 面板消费（api.py）——web 关闭时不构造也不
    # import 依赖链（纯净度插件模块/geoip2 均分支内惰性导入）
    purity = _load_ippure(store)
    # IP 归属地图：GeoLite2 离线定位 + SQLite 缓存（Web 面板地图页面按需懒查）
    # geoip.enabled=false 或 web 关闭时不构造（不 import geoip2 链、不加载 mmdb）
    # 定位库切换（City/Country 或版本升级）时指纹变化，清空旧缓存防止粒度不一致数据复用
    geo: IpGeoUseCase | None = None
    if config.GEOIP_ENABLED and config.WEB_ENABLED:
        from src.interfaces.geoip_client import GeoLite2Locator
        try:
            geo_locator = GeoLite2Locator(config.GEOIP_DB_FILE)
        except Exception as exc:
            # 库不随仓库分发：缺失/损坏时只禁用地图，serve 仍可运行。
            # 下载 GeoLite2-City/Country.mmdb 放到 vendor/geoip/，或在
            # panel.toml 里把 geoip.db_file 指向实际位置
            logger.warning(
                "GeoLite2 加载失败，IP 归属地图禁用 path=%s err=%s",
                config.GEOIP_DB_FILE,
                exc,
            )
        else:
            if store.get_geo_cache_fingerprint() != geo_locator.fingerprint:
                store.clear_geo_cache()
                store.set_geo_cache_fingerprint(geo_locator.fingerprint)
                logger.info("GeoLite2 定位库变化，已清空 geo 缓存")
            geo = IpGeoUseCase(
                locator=geo_locator,
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
