"""应用配置：config/ 目录下按领域拆分的多个 TOML（固定文件名，无 include/合并机制）

    config.toml   [paths] [web]        路径与面板开关
    pool.toml     [pool] [groups]      池策略与分组定义
    sources.toml  [fetch] [[sources]]  代理源与拉取调度
    serve.toml    [server]             serve 端口/队列/心跳/链路门控/metrics
    panel.toml    [purity] [geoip]     面板功能（纯洁度/归属地图）
    logging.toml  [logging]            日志双通道级别分档 + 逐 logger 覆盖
    apikey.env    密钥（KEY=VALUE，git 忽略）

每个顶层节固定归属一个文件：节放错文件直接报错（防静默失效），缺文件报错。
本模块在导入时加载并导出模块级常量（PROXY_GROUPS/TRUSTED_GROUP/SOURCES/
SERVER_* 等），组合根与各框架层模块只从这里导入，不直接接触 TOML。
组全部由 pool.toml [groups] 定义（仅 ttl），可请求组由 [pool].requestable_groups
显式声明，特殊语义组（trusted/blacklist/standby）由 [pool] 与 [server] 显式
指定组名，代码不硬编码组名。路径类配置相对项目根解析。

领域规则常量经组合根（container.py）注入到 ProxyPool 与各用例；
CACHE_DB/LOG_DIR 为框架层路径。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # Python 3.8 兼容

# config.py 位于 src/framework/，上溯 3 级到项目根
_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# PROXY_POOL_CONFIG 环境变量指向替代配置目录（e2e 测试隔离用：临时 cache_db、
# 本地 mock 探测 URL、独立端口）；缺省为项目 config/。目录内文件须齐全（固定清单，无回退）
CONFIG_DIR = Path(os.environ.get("PROXY_POOL_CONFIG") or (_PROJECT_ROOT / "config"))

# 文件名 -> 该文件允许的顶层节。节跨文件放置视为配置错误（避免"放错文件静默失效"）。
_SECTION_OWNERSHIP: dict[str, set[str]] = {
    "config.toml": {"paths", "web"},
    "pool.toml": {"pool", "groups"},
    "sources.toml": {"fetch", "sources"},
    "serve.toml": {"server"},
    "panel.toml": {"purity", "geoip"},
    "logging.toml": {"logging"},
}


def _load_all() -> dict:
    """加载全部领域配置文件，合并为单一节字典；缺失/解析失败/节越界直接报错"""
    merged: dict = {}
    for filename, allowed in _SECTION_OWNERSHIP.items():
        path = CONFIG_DIR / filename
        with open(path, "rb") as f:  # 缺文件 FileNotFoundError 即快速失败
            data = tomllib.load(f)
        misplaced = set(data) - allowed
        if misplaced:
            raise ValueError(
                f"{filename} 含不属于本文件的节 {sorted(misplaced)}；"
                f"各节归属：{_SECTION_OWNERSHIP}"
            )
        merged.update(data)
    return merged


def _parse_ttl(value) -> int | None:
    """解析 TOML 中的 TTL 值：整数秒数；"never" 表示永不过期"""
    if isinstance(value, str):
        if value == "never":
            return None
        raise ValueError(f"非法 TTL 值 {value!r}：应为秒数或 \"never\"")
    return int(value)


_toml = _load_all()

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
# serve 默认代理协议（CLI 未指定 --type 时的初始值；运行时由 ServeControl 持有，面板可增删）。
# 不含 https：https 代理在 pproxy 中是 TLS 终结（http+ssl:// 上游），浏览器访问 https
# 目标会拿到上游证书导致 ERR_CERT_AUTHORITY_INVALID；http/socks5 均为透传，无此问题。
SERVER_PROXY_TYPES: tuple[str, ...] = tuple(_toml["server"]["proxy_types"])
# 本地链路门控（serve 心跳/补测/fill 探测前后各 check 一次本地链路，任何一次不可用即跳过本轮/不计分避免误拉黑）
NETWORK_PROBE_URLS: tuple[str, ...] = tuple(_toml["server"]["network_probe_urls"])  # 探测目标（任一返回 <400 即正常）
NETWORK_PROBE_TIMEOUT = float(_toml["server"]["network_probe_timeout"])             # 单个目标超时秒数
# 无探测目标则链路永远判为不可用（serve 静默停摆），配置错误直接在启动时暴露
if not NETWORK_PROBE_URLS:
    raise ValueError("serve.toml [server].network_probe_urls 不可为空（链路门控无探测目标）")

# ---------------------------------------------------------------- Web 控制面板（serve 模式）
WEB_ENABLED = bool(_toml["web"].get("enabled", True))  # 面板总开关；false 时 serve 不构造/启动面板
WEB_HOST = _toml["web"]["host"]   # 面板监听地址（默认仅本机）
WEB_PORT = int(_toml["web"]["port"])  # 面板监听端口

# ---------------------------------------------------------------- 缓存与日志（相对项目根解析）
CACHE_DB = str(_PROJECT_ROOT / _toml["paths"]["cache_db"])     # 代理池状态持久化 SQLite 数据库（WAL 模式，组级增量 upsert）
LOG_DIR = str(_PROJECT_ROOT / _toml["paths"]["log_dir"])         # 日志目录（文件名为 proxy-pool-{时间戳}.log，毫秒精度）
AUTOLOAD_FILE = str(_PROJECT_ROOT / _toml["paths"]["autoload"])  # 启动时自动加载的代理清单文件（一行一个，# 注释；不存在则跳过）

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

# ---------------------------------------------------------------- AbuseIPDB（IP 纯洁度检查）
# API key 从配置目录的 apikey.env 加载（敏感信息不入 TOML，避免 git 追踪）。
# 文件格式：KEY=VALUE 每行一个，# 开头为注释。key 缺失时纯洁度检查功能禁用。
_APIKEY_FILE = CONFIG_DIR / "apikey.env"
ABUSEIPDB_API_KEY: str | None = None
if _APIKEY_FILE.exists():
    with open(_APIKEY_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            if _k.strip() == "ABUSEIPDB_API_KEY":
                ABUSEIPDB_API_KEY = _v.strip()
                break
# 查询窗口（天）：只统计该天数内的举报，AbuseIPDB 允许 1-365
ABUSEIPDB_MAX_AGE_DAYS = 90
# 缓存有效期（秒）：30 天内同 IP 直接返回缓存，避免重复调 API 消耗日限额
ABUSEIPDB_CACHE_TTL = 30 * 86400
# 各源代理（被墙时用），空字符串表示直连
_purity_cfg = _toml.get("purity", {})
PURITY_SOURCE = _purity_cfg.get("source", "ipok")   # 检查源：ipok / abuseipdb
IPOK_PROXY = _purity_cfg.get("ipok_proxy", "")
ABUSEIPDB_PROXY = _purity_cfg.get("abuseipdb_proxy", "")
# key 缺失只警告：由组合根禁用纯洁度检查，不阻断启动（非 abuseipdb 源无需该文件，保持静默）
if PURITY_SOURCE == "abuseipdb" and not ABUSEIPDB_API_KEY:
    _logger.warning(
        "未读取到 ABUSEIPDB_API_KEY（%s 缺失或未配置该键），纯洁度检查将禁用",
        _APIKEY_FILE,
    )

# ---------------------------------------------------------------- IP 归属地（Web 地图）
_geoip_cfg = _toml.get("geoip", {})
GEOIP_ENABLED = bool(_geoip_cfg.get("enabled", True))  # 地图/归属定位开关；false 时不加载 mmdb 定位器
GEOIP_DB_FILE = str(_PROJECT_ROOT / _geoip_cfg.get("db_file", "vendor/geoip/GeoLite2-City.mmdb"))
GEOIP_CACHE_TTL = int(_geoip_cfg.get("cache_ttl", 7776000))  # 默认 90 天
