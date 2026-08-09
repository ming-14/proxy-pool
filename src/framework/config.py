"""应用配置：唯一数据源为 config/config.toml（项目根下）

本模块在导入时加载 TOML，并导出模块级常量（PROXY_GROUPS/TRUSTED_GROUP/
MAX_CONSECUTIVE_FAILS/SOURCES/SERVER_* 等），组合根与各框架层模块只从这里导入，
不直接接触 TOML。组全部由 [groups] 定义（仅 ttl），可请求组由
[pool].requestable_groups 显式声明，特殊语义组（trusted/blacklist/standby）
由 [pool] 与 [server] 显式指定组名，代码不硬编码组名。路径类配置
（cache_file/log_dir）相对项目根解析。

领域规则常量经组合根（container.py）注入到 ProxyPool 与各用例；
CACHE_DB/LOG_DIR 为框架层路径。
"""

import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # Python 3.10 兼容

# config.py 位于 src/framework/，上溯 3 级到项目根
_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONFIG_FILE = _PROJECT_ROOT / "config" / "config.toml"


def _load_toml() -> dict:
    """读取 TOML 配置；文件缺失或解析失败直接报错（配置是硬性要求）"""
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def _parse_ttl(value) -> int | None:
    """解析 TOML 中的 TTL 值：整数秒数；"never" 表示永不过期"""
    if isinstance(value, str):
        if value == "never":
            return None
        raise ValueError(f"非法 TTL 值 {value!r}：应为秒数或 \"never\"")
    return int(value)


_toml = _load_toml()

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
# 多阶段爬取的并发/限流/超时/请求头，仅该源使用（置于全局配置以遵循唯一配置数据源原则）。
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
HEARTBEAT_PROBE_URL = _toml["server"]["heartbeat_probe_url"]     # 心跳探测目标 URL
HEARTBEAT_CONCURRENCY = int(_toml["server"]["heartbeat_concurrency"])  # 心跳探测并发数
HEARTBEAT_TIMEOUT = int(_toml["server"]["heartbeat_timeout"])    # 心跳探测单个代理超时秒数
# 队列指标采样存储时长秒（窗口）。每轮心跳采一次，超出窗口的自动清理；心跳动态调整时窗口始终不变。
METRICS_WINDOW = int(_toml["server"]["metrics_window"])
# 不含 https：https 代理在 pproxy 中是 TLS 终结（http+ssl:// 上游），浏览器访问 https
# 目标会拿到上游证书导致 ERR_CERT_AUTHORITY_INVALID；http/socks5 均为透传，无此问题。
SERVE_PROXY_TYPES: tuple[str, ...] = tuple(_toml["server"]["serve_proxy_types"])
# 本地网络门控（serve 心跳/补测/fill 前直连探测判定本地网络是否可用，异常时跳过避免误拉黑）
NETWORK_PROBE_URLS: tuple[str, ...] = tuple(_toml["server"]["network_probe_urls"])  # 探测目标（任一通即正常）
NETWORK_CHECK_CACHE_OK = float(_toml["server"]["network_check_cache_ok"])           # 正常时缓存秒数
NETWORK_CHECK_BACKOFF_INITIAL = float(_toml["server"]["network_check_backoff_initial"])  # 异常时退避起始间隔
NETWORK_CHECK_BACKOFF_MAX = float(_toml["server"]["network_check_backoff_max"])    # 退避间隔上限

# ---------------------------------------------------------------- Web 控制面板（serve 模式）
WEB_HOST = _toml["web"]["host"]   # 面板监听地址（默认仅本机）
WEB_PORT = int(_toml["web"]["port"])  # 面板监听端口

# ---------------------------------------------------------------- 缓存与日志（相对项目根解析）
CACHE_DB = str(_PROJECT_ROOT / _toml["paths"]["cache_db"])     # 代理池状态持久化 SQLite 数据库（WAL 模式，组级增量 upsert）
LOG_DIR = str(_PROJECT_ROOT / _toml["paths"]["log_dir"])         # 日志目录（文件名为 proxy-pool-{时间戳}.log，毫秒精度）
