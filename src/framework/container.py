"""组合根：组装领域池、存储、用例，并向 CLI 暴露依赖

依赖方向：framework 层 import 所有内层（interfaces/use_cases/domain），
内层不 import 本模块。

重依赖惰性导入：纯净度检查插件（requests 链）与 GeoLite2 定位器（geoip2 链）
仅在对应功能启用的分支内 import——serve 稳态（心跳/门控走 aiohttp）与
geoip.enabled=false 场景不加载这些依赖树（边际 ~5.6MB，调研见会话记录）。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from src.domain.models import SourceSpec, FpwConfig
from src.domain.ports import ProxySource, ProxySpeedTester
from src.domain.purity import IppurePluginConfig, IppureSourceSpec, resolve_proxy
from src.domain.proxy_pool import ProxyPool
from src.interfaces.http_client import HttpGetter, AsyncHttpGetter
from src.interfaces.proxy_scheme import verify_target
from src.interfaces.speedtest import PspeedTester
from src.interfaces.sqlite_store import SqliteProxyStore
from src.interfaces.scheduler import ProxyScheduler
from src.use_cases.autoload import AutoloadUseCase
from src.use_cases.check_proxies import CheckProxiesUseCase
from src.use_cases.check_purity import CheckPurityUseCase
from src.use_cases.enqueue_filter import EnqueueFilterUseCase, MetricProvider
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
    standby: StandbyQueueUseCase
    enqueue_filter: EnqueueFilterUseCase  # 入队筛选规则链（standby.fill 消费；按优先级惰性判定）
    async_getter: AsyncHttpGetter
    purity: CheckPurityUseCase | None   # IP 纯净度检查；web 关闭/未启用源/缺密钥时为 None（功能禁用）
    geo: IpGeoUseCase | None          # IP 归属地图（GeoLite2 定位 + 缓存 + 聚合）；geoip.enabled=false 时为 None
    speedtest: PspeedTester            # 测速（面板手动测速 + 入队筛选测速指标）

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


def _speed_provider(tester: ProxySpeedTester) -> MetricProvider:
    """测速测量源：同步阻塞实现放 executor，不阻塞 serve 事件循环"""
    async def provider(proxy: str) -> object | None:
        return await asyncio.get_running_loop().run_in_executor(None, tester.download_mbps, proxy)

    return provider


def _enqueue_metric_enabled(metric: str) -> bool:
    """配置里是否有启用该指标的入队规则（决定无面板时是否构造其测量源）

    web 开启时构造条件更宽（面板可运行时开启任意规则），这里只覆盖"没有面板也必须测"的场景。
    """
    from src.framework import config

    return any(rule.enabled and rule.metric == metric for rule in config.ENQUEUE_RULES)


def _load_geo(store: SqliteProxyStore) -> IpGeoUseCase | None:
    """加载 GeoLite2 定位器并组装 IP 归属用例（Web 面板与入队筛选消费）

    库不随仓库分发：缺失/损坏时只警告并禁用（serve 仍可运行）。定位库切换（City/Country
    或版本升级）时指纹变化，清空旧缓存防止粒度不一致数据复用。不需要归属数据时不 import
    geoip2 链、不加载 mmdb。
    """
    from src.framework import config

    if not config.GEOIP_ENABLED or not (config.WEB_ENABLED or _enqueue_metric_enabled("country")):
        return None
    from src.interfaces.geoip_client import GeoLite2Locator
    try:
        locator = GeoLite2Locator(config.GEOIP_DB_FILE)
    except Exception as exc:
        # 下载 GeoLite2-City/Country.mmdb 放到 vendor/geoip/，或在 panel.toml 里把
        # geoip.db_file 指向实际位置
        logger.warning("GeoLite2 加载失败，IP 归属禁用 path=%s err=%s", config.GEOIP_DB_FILE, exc)
        return None
    if store.get_geo_cache_fingerprint() != locator.fingerprint:
        store.clear_geo_cache()
        store.set_geo_cache_fingerprint(locator.fingerprint)
        logger.info("GeoLite2 定位库变化，已清空 geo 缓存")
    return IpGeoUseCase(locator=locator, cache=store, cache_ttl=config.GEOIP_CACHE_TTL)


def _load_ippure(store: SqliteProxyStore) -> CheckPurityUseCase | None:
    """加载纯净度检查插件并组装用例（Web 面板与入队筛选消费）

    配置错误（插件模块导入失败/缺 SOURCE/插件名重复/节未被声明/未知源名/插件参数非法）
    直接抛错——启动即暴露，不做运行期兜底；选中源缺必需环境变量时只警告并禁用，
    不阻断启动。插件模块仅在本函数内 import，保持依赖链惰性。
    """
    from src.framework import config

    if not config.IPPURE_SOURCE or not (config.WEB_ENABLED or _enqueue_metric_enabled("purity")):
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
    dropped = pool.restore_state(store.load())
    if dropped:
        # 协议支持集合收窄（如移除 socks4）后，历史入库条目在此被丢弃——不静默
        logger.warning("恢复代理池时丢弃 %d 个协议不受支持的条目", dropped)
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
        filter_rules=config.FETCH_FILTER_RULES,
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
        verify_policy=verify_target,
    )
    network_gate = LocalNetworkGate(
        async_getter=async_getter,
        probe_urls=config.NETWORK_PROBE_URLS,
        probe_timeout=config.NETWORK_PROBE_TIMEOUT,
        unhealthy_cache_ttl=config.NETWORK_UNHEALTHY_CACHE_TTL,
    )
    # IP 纯净度 / IP 归属：Web 面板与入队筛选消费——两者均分支内惰性 import 依赖链
    purity = _load_ippure(store)
    geo = _load_geo(store)
    # 入队筛选测量源：latency 直接取 fill 探测结果（不占 provider），其余指标按注册表提供
    speedtest = PspeedTester()
    providers: dict[str, MetricProvider] = {"speed": _speed_provider(speedtest)}
    if geo is not None:
        providers["country"] = geo.resolve_country
    if purity is not None:
        providers["purity"] = purity.resolve_score
    enqueue_filter = EnqueueFilterUseCase(rules=config.ENQUEUE_RULES, providers=providers)
    standby = StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check_proxies,
        network_gate=network_gate,
        enqueue_filter=enqueue_filter,
        standby_group=config.STANDBY_GROUP,
        reject_group=config.ENQUEUE_REJECT_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
        metrics_window=config.METRICS_WINDOW,
        chart_window=config.STANDBY_CHART_WINDOW,
    )
    return Container(
        pool=pool,
        store=store,
        fetch=fetch,
        get_proxies=get_proxies,
        record=record,
        request_with_retry=request_with_retry,
        check_proxies=check_proxies,
        standby=standby,
        enqueue_filter=enqueue_filter,
        async_getter=async_getter,
        purity=purity,
        geo=geo,
        speedtest=speedtest,
    )
