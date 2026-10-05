"""应用配置：config/ 目录下按领域拆分的多个 TOML（固定文件名，无 include/合并机制）

    config.toml   [paths] [web]               路径与面板开关
    pool.toml     [pool] [groups]             池策略与分组定义
    sources.toml  [fetch] [[sources]] [[fetch_filter]]   代理源、拉取调度与入池筛选规则
    serve.toml    [server]                    serve 端口/队列/心跳/链路门控/metrics
    panel.toml    [geoip] [globe] [traffic]   面板功能（IP 归属地图 / 3D 地球 / 流量统计）
    ippure.toml   [ippure] [[plugins]] ...    IP 纯净度检查（插件节名由插件自声明）
    logging.toml  [logging]                   日志双通道级别分档 + 逐 logger 覆盖
    .env          密钥（KEY=VALUE，git 忽略）——加载时注入进程环境变量

每个顶层节固定归属一个文件：节放错文件/跨文件重复直接报错（防静默失效），缺文件报错。
本模块在导入时加载并导出模块级常量（PROXY_GROUPS/TRUSTED_GROUP/SOURCES/
SERVER_* 等），组合根与各框架层模块只从这里导入，不直接接触 TOML。
组全部由 pool.toml [groups] 定义（仅 ttl），可请求组由 [pool].requestable_groups
显式声明，特殊语义组（trusted/blacklist/standby）由 [pool] 与 [server] 显式
指定组名，代码不硬编码组名。[paths] 下 cache_db/log_dir/autoload 相对数据根解析
（PROXY_POOL_DATA 可覆盖，缺省即项目根）；只读资源（vendor/geoip）相对代码根（项目根）解析。

领域规则常量经组合根（container.py）注入到 ProxyPool 与各用例；
CACHE_DB/LOG_DIR 为框架层路径。
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from pathlib import Path

_logger = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # Python 3.8 兼容

from src.domain.enqueue_filter import METRIC_SPECS, EnqueueRule  # noqa: E402
from src.domain.models import SUPPORTED_SCHEMES  # noqa: E402
from src.domain.purity import ProxyRule  # noqa: E402  (tomllib 分支之后，保持导入分组清晰)
from src.domain.source_filter import FilterRule  # noqa: E402

# config.py 位于 src/framework/，上溯 3 级到项目根
_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# 代码根与运行数据根分离：源码/随包资源（vendor、vendor/geoip）可能位于只读目录
# （如内嵌发行的 resources/），而 cache_db/日志/autoload 必须可写落到用户可写位置。
# PROXY_POOL_DATA 覆盖数据根（桌面封装用：指向 exe 同级目录）；缺省回退代码根，
# 故 CLI/测试行为不变。注意 vendor/geoip 等只读资源仍按代码根解析，不受此覆盖影响。
_DATA_ROOT = Path(os.environ.get("PROXY_POOL_DATA") or _PROJECT_ROOT)
# PROXY_POOL_CONFIG 环境变量指向替代配置目录（e2e 测试隔离用：临时 cache_db、
# 本地 mock 探测 URL、独立端口）；缺省为项目 config/。目录内文件须齐全（固定清单，无回退）
CONFIG_DIR = Path(os.environ.get("PROXY_POOL_CONFIG") or (_PROJECT_ROOT / "config"))

# ---------------------------------------------------------------- 环境变量注入（config/.env）
# 密钥统一走环境变量：加载配置前把 .env 注入进程环境（KEY=VALUE，值可带引号），
# 已存在的真实环境变量优先、不被覆盖；缺失 .env 静默（密钥可由真实环境变量提供）。
_ENV_FILE = CONFIG_DIR / ".env"


def _inject_env_file(path: Path) -> int:
    """把 .env 注入 os.environ，返回注入条数；非法行直接报错（含行号，防静默失效）"""
    if not path.exists():
        return 0
    injected = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{path} 第 {lineno} 行不是 KEY=VALUE 格式: {raw!r}")
        if not os.environ.get(key):
            os.environ[key] = value.strip().strip("'\"")
            injected += 1
    return injected


_injected_env_count = _inject_env_file(_ENV_FILE)
if _ENV_FILE.exists():
    _logger.info("已从 %s 注入 %d 个环境变量（已存在的变量不覆盖）", _ENV_FILE, _injected_env_count)

# 文件名 -> 该文件允许的顶层节。节跨文件放置/跨文件重名视为配置错误（避免"放错文件静默失效"）。
# ippure.toml 的插件节名由插件自声明（任意名），用 _PLUGIN_SECTIONS 哨兵豁免本层校验，
# 容器层再按插件元数据核对（见 container._load_ippure）。
_PLUGIN_SECTIONS = object()
_SECTION_OWNERSHIP: dict[str, set] = {
    "config.toml": {"paths", "web"},
    "pool.toml": {"pool", "groups"},
    "sources.toml": {"fetch", "sources", "fetch_filter"},
    "serve.toml": {"server"},
    "panel.toml": {"geoip", "globe", "traffic"},
    "ippure.toml": {"ippure", "plugins", "proxy_rules", _PLUGIN_SECTIONS},
    "logging.toml": {"logging"},
}


def _load_all() -> tuple[dict, dict[str, dict]]:
    """加载全部领域配置文件

    返回 (合并节字典, 各文件原始节字典)；缺失/解析失败/节越界/跨文件重名直接报错。
    合并前先查跨文件重名：两文件同名顶层节会互相覆盖，必须显式暴露而不是静默丢一份。
    """
    merged: dict = {}
    per_file: dict[str, dict] = {}
    for filename, allowed in _SECTION_OWNERSHIP.items():
        path = CONFIG_DIR / filename
        with open(path, "rb") as f:  # 缺文件 FileNotFoundError 即快速失败
            data = tomllib.load(f)
        duplicated = set(data) & set(merged)
        if duplicated:
            raise ValueError(f"{filename} 与其它配置文件重复定义节 {sorted(duplicated)}")
        if _PLUGIN_SECTIONS not in allowed:
            misplaced = set(data) - allowed
            if misplaced:
                raise ValueError(
                    f"{filename} 含不属于本文件的节 {sorted(misplaced)}；"
                    f"本文件允许的节 {sorted(allowed)}"
                )
        merged.update(data)
        per_file[filename] = data
    return merged, per_file


def _parse_ttl(value) -> int | None:
    """解析 TOML 中的 TTL 值：整数秒数；"never" 表示永不过期"""
    if isinstance(value, str):
        if value == "never":
            return None
        raise ValueError(f"非法 TTL 值 {value!r}：应为秒数或 \"never\"")
    return int(value)


_toml, _toml_files = _load_all()

# ---------------------------------------------------------------- 代理分组
# 组全部由 [groups] 定义（组名 -> {ttl}），仅声明 ttl：
#   ttl:  整数秒数；"never" 永不过期（None）
# 可请求组由 [pool].requestable_groups 显式声明，特殊语义组由 [pool] 与 [server] 指定组名。
_GROUPS: dict[str, dict] = _toml["groups"]

# 组名 -> TTL 秒数；None 表示永不过期。
# 代理加入组时记录 joined_at，超过 TTL 自动清除（见 ProxyPool._cleanup_all_expired）。
PROXY_GROUPS: dict[str, int | None] = {
    name: _parse_ttl(spec["ttl"]) for name, spec in _GROUPS.items()
}

# 实际访问网站时可用的组（request 命令从中取代理，get_available_proxies 遍历）。
# 显式列表声明（[pool].requestable_groups），未列出的组（如 blacklist）不参与可用集合。
REQUESTABLE_GROUPS: tuple[str, ...] = tuple(_toml["pool"]["requestable_groups"])
_unknown_requestable = [g for g in REQUESTABLE_GROUPS if g not in _GROUPS]
if _unknown_requestable:
    raise ValueError(f"requestable_groups 含未定义的组: {_unknown_requestable}")


TRUSTED_GROUP = _toml["pool"]["trusted_group"]       # 信任池：成功使用过的代理（晋级目标，永不过期）
BLACKLIST_GROUP = _toml["pool"]["blacklist_group"]   # 黑名单：失败淘汰代理的归属，不可请求
for _g in (TRUSTED_GROUP, BLACKLIST_GROUP):
    if _g not in _GROUPS:
        raise ValueError(f"组 {_g!r} 未在 [groups] 定义")

# ---------------------------------------------------------------- 连续失败淘汰阈值
# consecutive_fails 为连续失败计数：每次失败 +1，任何成功（请求成功/心跳成功/补测成功）清零。
MAX_CONSECUTIVE_FAILS = int(_toml["pool"]["max_consecutive_fails"])  # 普通组代理连续失败次数 >= 该值后不再参与可用集合
TRUSTED_MAX_CONSECUTIVE_FAILS = int(_toml["pool"]["trusted_max_consecutive_fails"])  # trusted 组代理的连续失败上限

# ---------------------------------------------------------------- 代理源配置
# name 为源模块导入路径，也是源暂停记录 key（ProxyPool.source_paused）。
# group 决定拉取到的代理写入哪个组；group_by_protocol 源按代理 scheme 前缀分拣到
# group-{scheme}（无 scheme 前缀按 http），如 "default" -> default-http/https/socks5。
# pause_ttl 为拉取完成后强制暂停该源的时长；None 表示仅当去重率超阈值时才暂停。
SOURCES = [
    {
        "name": s["name"],
        "group": s["group"],
        "enabled": bool(s["enabled"]),
        "pause_ttl": int(s["pause_ttl"]) if s.get("pause_ttl") is not None else None,
        "group_by_protocol": bool(s.get("group_by_protocol", False)),
    }
    for s in _toml["sources"]
]

# ---------------------------------------------------------------- 拉取调度
SOURCE_PAUSE_TTL = int(_toml["fetch"]["source_pause_ttl"])      # 源暂停默认时长（去重率过高时使用）
SOURCE_DEDUP_THRESHOLD = float(_toml["fetch"]["source_dedup_threshold"])  # 源去重率阈值，超过则暂停该源
FETCH_PROXY_RETRIES = int(_toml["fetch"]["fetch_proxy_retries"])  # 每个代理源拉取时的最大重试次数


# ---------------------------------------------------------------- 入池筛选规则
def _parse_filter_rule(raw: dict, index: int) -> FilterRule:
    """校验单条筛选规则（键/正则非法直接报错，避免错误规则静默失效）"""
    label = f"sources.toml [[fetch_filter]] 第 {index + 1} 条"
    unknown = set(raw) - {"pattern", "reason", "match"}
    if unknown:
        raise ValueError(f"{label} 含未知键 {sorted(unknown)}（可选 pattern/reason/match）")
    pattern = str(raw.get("pattern", "")).strip()
    if not pattern:
        raise ValueError(f"{label} 缺少 pattern（代理 URL 正则）")
    reason = str(raw.get("reason", "")).strip()
    if not reason:
        raise ValueError(f"{label} 缺少 reason（命中原因，用于日志）")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{label} pattern 非法 {pattern!r}: {exc}") from exc
    return FilterRule(pattern=compiled, reason=reason,
                      source_glob=str(raw.get("match", "")).strip() or "*")


FETCH_FILTER_RULES: tuple[FilterRule, ...] = tuple(
    _parse_filter_rule(rule, i) for i, rule in enumerate(_toml.get("fetch_filter") or [])
)

# ---------------------------------------------------------------- FreeProxy.World 源专属参数
# 多阶段爬取的并发/限流/超时/请求头，仅该源使用（置于 sources.toml，遵循领域配置归属）。
# 框架层导出，组合根（container._source_loader）组装为 interfaces 层的 FpwConfig 注入源工厂，
# interfaces 层不直接依赖本模块。
_fpw = _toml["fetch"]["freeproxy_world"]
FPW_MAX_CONCURRENT = int(_fpw["max_concurrent"])        # 并发爬取页面的最大并发数
FPW_PROXY_RETRY = int(_fpw["proxy_retry"])              # 单代理连续失败上限（超过丢弃）
FPW_TASK_INTERVAL = int(_fpw["task_interval"])          # 任务间隔秒（限流）
FPW_REQUEST_TIMEOUT = int(_fpw["request_timeout"])      # 单页面请求超时秒
FPW_ACCEPT_LANGUAGE = _fpw["accept_language"]           # Accept-Language 头（中性值，不暴露地理线索）

# ---------------------------------------------------------------- 本地代理服务器（serve 命令）
SERVER_HOST = _toml["server"]["host"]                            # 监听地址
SERVER_PORT = int(_toml["server"]["port"])                       # 监听端口
# 入站监听协议（客户端侧）：拼 pproxy 监听 URI 的协议段，与 proxy_types（出站过滤）无关。
INBOUND_PROTOCOLS: tuple[str, ...] = ("http", "socks4", "socks5")  # pproxy 入站协议（https 需证书，未启用）
SERVER_LISTEN_PROTOCOLS: tuple[str, ...] = tuple(_toml["server"]["listen_protocols"])
if not SERVER_LISTEN_PROTOCOLS:
    raise ValueError("serve.toml [server].listen_protocols 不可为空（入站无监听协议）")
_unknown_listen = [p for p in SERVER_LISTEN_PROTOCOLS if p not in INBOUND_PROTOCOLS]
if _unknown_listen:
    raise ValueError(
        f"serve.toml [server].listen_protocols 含未知协议 {_unknown_listen}（可选 {list(INBOUND_PROTOCOLS)}）"
    )
STANDBY_GROUP = _toml["server"]["standby_group"]                 # serve 代理服务队列组（pproxy 上游，由心跳探测维护）
if STANDBY_GROUP not in _GROUPS:
    raise ValueError(f"组 {STANDBY_GROUP!r} 未在 [groups] 定义")
STANDBY_QUEUE_CAPACITY = int(_toml["server"]["standby_queue_capacity"])  # 代理服务队列容量上限
# 代理服务队列填充候选数：缺口 × 倍率，且至少 缺口 + 最小增量（提高存活命中率）
STANDBY_FILL_PROBE_MULTIPLIER = int(_toml["server"]["standby_fill_probe_multiplier"])
STANDBY_FILL_PROBE_MIN_EXTRA = int(_toml["server"]["standby_fill_probe_min_extra"])
# 代理服务队列填充候选组（按优先级顺序，靠前优先；trusted 排最前保证已验证代理优先入队）
STANDBY_FILL_GROUPS: tuple[str, ...] = tuple(_toml["server"]["standby_fill_groups"])
_unknown_fill_groups = [g for g in STANDBY_FILL_GROUPS if g not in _GROUPS]
if _unknown_fill_groups:
    raise ValueError(f"standby_fill_groups 含未定义的组: {_unknown_fill_groups}")
# ---------------------------------------------------------------- 入队筛选规则
# 候选入队前按 priority 升序逐条判定（小号先测），未通过即移入 enqueue_reject_group。
# 指标参数键与校验由 domain.METRIC_SPECS 提供（新增指标只改领域层）。
_ENQUEUE_META_KEYS = {"metric", "priority", "enabled"}


def _parse_enqueue_rule(raw: dict, index: int) -> EnqueueRule:
    """校验单条入队筛选规则（未知指标/键、缺参数、优先级非法直接报错）"""
    label = f"serve.toml [[server.enqueue_rules]] 第 {index + 1} 条"
    metric = str(raw.get("metric", "")).strip()
    spec = METRIC_SPECS.get(metric)
    if spec is None:
        raise ValueError(f"{label} 未知指标 {metric!r}（可选 {sorted(METRIC_SPECS)}）")
    allowed = _ENQUEUE_META_KEYS | set(spec.required) | set(spec.optional)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} 含未知键 {sorted(unknown)}（可选 {sorted(allowed)}）")
    missing = spec.required - set(raw)
    if missing:
        raise ValueError(f"{label} 缺少必需参数 {sorted(missing)}")
    priority = raw.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority <= 0:
        raise ValueError(f"{label} priority 须为正整数")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"{label} enabled 须为布尔值")
    return EnqueueRule(metric=metric, priority=priority, params=spec.coerce(raw), enabled=enabled)


ENQUEUE_RULES: tuple[EnqueueRule, ...] = tuple(
    _parse_enqueue_rule(rule, i) for i, rule in enumerate(_toml["server"].get("enqueue_rules") or [])
)
_repeated = sorted(key for key, count in Counter(r.priority for r in ENQUEUE_RULES).items() if count > 1)
if _repeated:
    raise ValueError(f"serve.toml [[server.enqueue_rules]] priority 重复: {_repeated}")
# 每个指标至多一条：网页设置按指标启停/改阈值，重复指标无法定位到具体一条
_repeated = sorted(key for key, count in Counter(r.metric for r in ENQUEUE_RULES).items() if count > 1)
if _repeated:
    raise ValueError(f"serve.toml [[server.enqueue_rules]] 指标重复: {_repeated}（每个指标至多一条）")

# 拒绝候选的归属组：必须已定义且不可请求
# （可请求会让被拒绝的代理立即重新成为候选，规则形同虚设）
ENQUEUE_REJECT_GROUP = str(_toml["server"]["enqueue_reject_group"])
if ENQUEUE_REJECT_GROUP not in _GROUPS:
    raise ValueError(f"组 {ENQUEUE_REJECT_GROUP!r} 未在 [groups] 定义")
if ENQUEUE_REJECT_GROUP in REQUESTABLE_GROUPS:
    raise ValueError(
        f"组 {ENQUEUE_REJECT_GROUP!r} 不可列入 pool.toml requestable_groups（被拒绝的候选会立即重新成为候选）"
    )

HEARTBEAT_INTERVAL = int(_toml["server"]["heartbeat_interval"])  # 心跳间隔秒
# 锁定单一上游时的心跳间隔秒（锁定时代理不淘汰不拉黑，仅测速展示，降低探测频率）
HEARTBEAT_LOCK_INTERVAL = int(_toml["server"]["heartbeat_lock_interval"])
HEARTBEAT_PROBE_URL = _toml["server"]["heartbeat_probe_url"]     # 心跳探测目标 URL
HEARTBEAT_CONCURRENCY = int(_toml["server"]["heartbeat_concurrency"])  # 心跳探测并发数
HEARTBEAT_TIMEOUT = int(_toml["server"]["heartbeat_timeout"])    # 心跳探测单个代理超时秒数
# 采样数据留存时长秒（全局）：流量与队列指标的采样点都按此清理，超出即丢弃。
METRICS_WINDOW = int(_toml["server"]["metrics_window"])
# 代理服务队列页图表的显示窗口秒数：只影响队列页行内趋势图的裁剪/画图范围，
# 不参与数据留存（留存统一由 METRICS_WINDOW 决定，也不影响仪表盘）。
STANDBY_CHART_WINDOW = int(_toml["server"]["standby_chart_window"])
# serve 默认上游代理类型（CLI 未指定 --type 时的初始值；运行时由 ServeControl 持有，面板可增删）。
# 只过滤进入代理服务队列的上游类型（standby.fill 候选筛选），不参与本地监听 URI 的构造
# （监听由 [server].listen_protocols 决定，见 SERVER_LISTEN_PROTOCOLS）。
SERVER_PROXY_TYPES: tuple[str, ...] = tuple(_toml["server"]["proxy_types"])
# 本地链路门控（serve 心跳/补测/fill 探测前后各 check 一次本地链路，任何一次不可用即跳过本轮/不计分避免误拉黑）
NETWORK_PROBE_URLS: tuple[str, ...] = tuple(_toml["server"]["network_probe_urls"])  # 探测目标（任一返回 <400 即正常）
NETWORK_PROBE_TIMEOUT = float(_toml["server"]["network_probe_timeout"])             # 单个目标超时秒数
# "链路不可用"结论复用时长秒（0 = 每次真实探测）：只缓存失败结论（成功结论陈旧会授权
# 误拉黑），缓存期内不再探测——同时是链路恢复的最大感知延迟，并抑制异常期的探测放大
NETWORK_UNHEALTHY_CACHE_TTL = float(_toml["server"]["network_unhealthy_cache_ttl"])
# 无探测目标则链路永远判为不可用（serve 静默停摆），配置错误直接在启动时暴露
if not NETWORK_PROBE_URLS:
    raise ValueError("serve.toml [server].network_probe_urls 不可为空（链路门控无探测目标）")

# ---------------------------------------------------------------- Web 控制面板（serve 模式）
WEB_ENABLED = bool(_toml["web"].get("enabled", True))  # 面板总开关；false 时 serve 不构造/启动面板
WEB_HOST = _toml["web"]["host"]   # 面板监听地址（默认仅本机）
WEB_PORT = int(_toml["web"]["port"])  # 面板监听端口

# ---------------------------------------------------------------- 缓存与日志（相对数据根解析）
CACHE_DB = str(_DATA_ROOT / _toml["paths"]["cache_db"])     # 代理池状态持久化 SQLite 数据库（WAL 模式，组级增量 upsert）
LOG_DIR = str(_DATA_ROOT / _toml["paths"]["log_dir"])         # 日志目录（文件名为 proxy-pool-{时间戳}.log，毫秒精度）
AUTOLOAD_FILE = str(_DATA_ROOT / _toml["paths"]["autoload"])  # 启动时自动加载的代理清单文件（一行一个，# 注释；不存在则跳过）

# ---------------------------------------------------------------- 日志级别（logging.toml）
# 控制台与文件独立分档；loggers 表为逐 logger 覆盖（两通道同时生效）。
_LOGGING_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level(name: str, value: str, where: str) -> int:
    key = str(value).upper()
    if key not in _LOGGING_LEVELS:
        raise ValueError(f"logging.toml {where} 非法级别 {value!r}（可选 {sorted(_LOGGING_LEVELS)}）")
    return getattr(logging, key)


_logging_cfg = _toml["logging"]
LOG_CONSOLE_LEVEL = _log_level("console_level", _logging_cfg.get("console_level", "DEBUG"), "console_level")
LOG_FILE_LEVEL = _log_level("file_level", _logging_cfg.get("file_level", "INFO"), "file_level")
# logger 名 -> 级别（如 aiohttp.access=WARNING 抑制面板轮询访问日志刷盘）
LOG_LOGGERS: dict[str, int] = {
    name: _log_level(name, level, f"loggers.{name}")
    for name, level in _logging_cfg.get("loggers", {}).items()
}

# ---------------------------------------------------------------- IP 纯净度检查（ippure.toml）
# 检查源为插件（src/interfaces/ippure/）：插件自声明配置节名（= SOURCE.name）与所需
# 环境变量，本层只做静态校验与透传，不认识任何具体源；[[plugins]] 列模块导入路径，
# 仅 [ippure].source 选中的插件会被组合根构造（见 container._load_ippure）。
_IPPURE_KEYS = {"source", "timeout", "cache_ttl"}
_ippure = _toml["ippure"]
_ippure_unknown = set(_ippure) - _IPPURE_KEYS
if _ippure_unknown:
    raise ValueError(f"ippure.toml [ippure] 含未知键 {sorted(_ippure_unknown)}（可选 {sorted(_IPPURE_KEYS)}）")
_ippure_missing = _IPPURE_KEYS - set(_ippure)
if _ippure_missing:
    raise ValueError(f"ippure.toml [ippure] 缺少必需键 {sorted(_ippure_missing)}")

# 启用插件名（= 插件 SOURCE.name = 该插件配置节名）；空字符串表示禁用纯净度检查
IPPURE_SOURCE = str(_ippure["source"]).strip()
IPPURE_TIMEOUT = float(_ippure["timeout"])   # 单次查询超时秒
IPPURE_CACHE_TTL = float(_ippure["cache_ttl"])  # 结果缓存有效期秒；0 = 不缓存
if IPPURE_TIMEOUT <= 0:
    raise ValueError(f"ippure.toml [ippure].timeout 须为正数: {IPPURE_TIMEOUT}")
if IPPURE_CACHE_TTL < 0:
    raise ValueError(f"ippure.toml [ippure].cache_ttl 不可为负: {IPPURE_CACHE_TTL}")

_plugins = _toml.get("plugins") or []
if not _plugins:
    raise ValueError("ippure.toml [[plugins]] 不可为空（至少列出可用的检查源模块路径）")
IPPURE_PLUGIN_MODULES: tuple[str, ...] = tuple(str(p.get("name", "")).strip() for p in _plugins)
if any(not name for name in IPPURE_PLUGIN_MODULES):
    raise ValueError('ippure.toml [[plugins]] 每项须为 name = "<模块导入路径>"')
if len(set(IPPURE_PLUGIN_MODULES)) != len(IPPURE_PLUGIN_MODULES):
    raise ValueError(f"ippure.toml [[plugins]] 模块路径重复: {sorted(IPPURE_PLUGIN_MODULES)}")

# 插件自声明配置节（ippure.toml 除固定节外的全部节），原样交给插件解析；
# 节名须与某个插件 SOURCE.name 一致，否则组合根报错（防拼错节名静默失效）
IPPURE_SECTIONS: dict[str, dict] = {
    name: section
    for name, section in _toml_files["ippure.toml"].items()
    if name not in {"ippure", "plugins", "proxy_rules"}
}

# 声明式代理规则：match 为插件名 glob，use 为代理 URL（空 = 直连）；按声明顺序取首个命中
_PROXY_SCHEMES = tuple(f"{s}://" for s in SUPPORTED_SCHEMES)  # 与池支持集合同源，避免两处漂移


def _parse_proxy_rule(rule: dict) -> ProxyRule:
    """校验单条代理规则（键/取值非法直接报错，避免错误规则静默失效）"""
    unknown = set(rule) - {"match", "use"}
    if unknown:
        raise ValueError(f"ippure.toml [[proxy_rules]] 含未知键 {sorted(unknown)}（可选 match/use）")
    match = str(rule.get("match", "")).strip()
    use = str(rule.get("use", "")).strip()
    if not match:
        raise ValueError("ippure.toml [[proxy_rules]] 缺少 match（插件名 glob）")
    if use and not use.startswith(_PROXY_SCHEMES):
        raise ValueError(
            f"ippure.toml [[proxy_rules]] use 非法 {use!r}"
            f"（须为 http/https/socks4/socks5 URL 或空字符串）"
        )
    return ProxyRule(match=match, use=use)


IPPURE_PROXY_RULES: tuple[ProxyRule, ...] = tuple(
    _parse_proxy_rule(rule) for rule in (_toml.get("proxy_rules") or [])
)

# ---------------------------------------------------------------- IP 归属地（Web 地图）
_geoip_cfg = _toml.get("geoip", {})
GEOIP_ENABLED = bool(_geoip_cfg.get("enabled", True))  # 地图/归属定位开关；false 时不加载 mmdb 定位器
GEOIP_DB_FILE = str(_PROJECT_ROOT / _geoip_cfg.get("db_file", "vendor/geoip/GeoLite2-City.mmdb"))
GEOIP_CACHE_TTL = int(_geoip_cfg.get("cache_ttl", 7776000))  # 默认 90 天

# ---------------------------------------------------------------- 3D 地球与目标地区探测（panel.toml）
# 依赖 geoip.enabled：地理库关闭时整个地球功能不可用（不构造用例、不注册端点）。
# 探测数据源是 PproxyServer 的 (bind, host) 流量维度；DoH 端点顺序即回退顺序。
_GLOBE_KEYS = {"enabled", "self_lat", "self_lng", "max_flows", "probe_concurrency",
               "probe_timeout", "target_cache_ttl", "activation_window",
               "probe_window", "prune_after", "doh_endpoints"}
# 默认 DoH 端点：Cloudflare/Google 走国际出口，alidns 供国内出口兜底（国外端点可能被阻断）
_DEFAULT_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
    "https://223.5.5.5/resolve",
)
_globe_cfg = _toml.get("globe", {})
_globe_unknown = set(_globe_cfg) - _GLOBE_KEYS
if _globe_unknown:
    raise ValueError(f"panel.toml [globe] 含未知键 {sorted(_globe_unknown)}（可选 {sorted(_GLOBE_KEYS)}）")

GLOBE_ENABLED = bool(_globe_cfg.get("enabled", True))
GLOBE_MAX_FLOWS = int(_globe_cfg.get("max_flows", 60))                    # 下发给地球的最大弧分组数
GLOBE_PROBE_CONCURRENCY = int(_globe_cfg.get("probe_concurrency", 3))    # DoH 探测并发上限
GLOBE_PROBE_TIMEOUT = float(_globe_cfg.get("probe_timeout", 8))          # 单次 DoH 超时秒
GLOBE_TARGET_CACHE_TTL = float(_globe_cfg.get("target_cache_ttl", 86400))  # (代理,目标) 缓存有效期秒
GLOBE_ACTIVATION_WINDOW = float(_globe_cfg.get("activation_window", 60))  # 面板活跃才探测的窗口秒
GLOBE_PROBE_WINDOW = float(_globe_cfg.get("probe_window", 600))           # 窗口内有流量才作为候选秒
GLOBE_PRUNE_AFTER = float(_globe_cfg.get("prune_after", 2592000))         # 缓存行清理阈值秒（30 天）
GLOBE_DOH_ENDPOINTS: tuple[str, ...] = tuple(
    str(endpoint).strip() for endpoint in _globe_cfg.get("doh_endpoints", _DEFAULT_DOH_ENDPOINTS)
)

# 本机坐标仅手填（不发起任何自动探测）；须成对配置，否则无法定位本机
_self_lat, _self_lng = _globe_cfg.get("self_lat"), _globe_cfg.get("self_lng")
if (_self_lat is None) != (_self_lng is None):
    raise ValueError("panel.toml [globe] self_lat/self_lng 须成对配置（只填一个无法定位本机）")
GLOBE_SELF_LAT: float | None = float(_self_lat) if _self_lat is not None else None
GLOBE_SELF_LNG: float | None = float(_self_lng) if _self_lng is not None else None
if GLOBE_SELF_LAT is not None and not -90 <= GLOBE_SELF_LAT <= 90:
    raise ValueError(f"panel.toml [globe].self_lat 越界: {GLOBE_SELF_LAT}（应在 -90..90）")
if GLOBE_SELF_LNG is not None and not -180 <= GLOBE_SELF_LNG <= 180:
    raise ValueError(f"panel.toml [globe].self_lng 越界: {GLOBE_SELF_LNG}（应在 -180..180）")

for _key, _value in (("max_flows", GLOBE_MAX_FLOWS), ("probe_concurrency", GLOBE_PROBE_CONCURRENCY),
                     ("probe_timeout", GLOBE_PROBE_TIMEOUT), ("target_cache_ttl", GLOBE_TARGET_CACHE_TTL),
                     ("activation_window", GLOBE_ACTIVATION_WINDOW), ("probe_window", GLOBE_PROBE_WINDOW),
                     ("prune_after", GLOBE_PRUNE_AFTER)):
    if _value <= 0:
        raise ValueError(f"panel.toml [globe].{_key} 须为正数: {_value}")
if not GLOBE_DOH_ENDPOINTS or any(not e.startswith(("http://", "https://")) for e in GLOBE_DOH_ENDPOINTS):
    raise ValueError(
        f"panel.toml [globe].doh_endpoints 须为非空 http(s) URL 列表: {GLOBE_DOH_ENDPOINTS!r}"
    )

# ---------------------------------------------------------------- 流量统计（panel.toml）
# 历史流量条目的保留期/落盘节流/下发上限；统计口径与内存窗口见 serve.toml 的 [server].metrics_window
_TRAFFIC_KEYS = {"history_limit", "flush_interval", "retention"}
_traffic_cfg = _toml.get("traffic", {})
_traffic_unknown = set(_traffic_cfg) - _TRAFFIC_KEYS
if _traffic_unknown:
    raise ValueError(
        f"panel.toml [traffic] 含未知键 {sorted(_traffic_unknown)}（可选 {sorted(_TRAFFIC_KEYS)}）"
    )

TRAFFIC_HISTORY_LIMIT = int(_traffic_cfg.get("history_limit", 500))       # 历史条目下发上限（按流量降序取）
TRAFFIC_FLUSH_INTERVAL = float(_traffic_cfg.get("flush_interval", 15))    # 历史条目落盘节流秒
TRAFFIC_RETENTION = float(_traffic_cfg.get("retention", 2592000))         # 历史条目保留期秒（超过即删）
for _key, _value in (("history_limit", TRAFFIC_HISTORY_LIMIT),
                     ("flush_interval", TRAFFIC_FLUSH_INTERVAL),
                     ("retention", TRAFFIC_RETENTION)):
    if _value <= 0:
        raise ValueError(f"panel.toml [traffic].{_key} 须为正数: {_value}")
