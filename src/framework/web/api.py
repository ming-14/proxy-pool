"""Web 控制面板 API：读（状态快照/组明细）+ 写（控制动作/调度策略调整）

框架层。所有写动作复用现有用例（fetch / record / standby）与领域方法，
不复制业务规则；只做「参数提取 + 校验 + 调用 + 落盘」的适配。

依赖注入：PanelContext 聚合 serve 运行时的容器组件、ServeControl 与
PproxyServer，aiohttp 路由经 _PANEL_KEY 取 context。serve 与面板共享同一
context 实例，保证监控与控制同一份状态。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable

from aiohttp import web

from src.domain.models import bind_of, normalize_proxy, proxy_to_ip
from src.domain.proxy_pool import LOCKED_GROUP
from src.framework import config, logbuffer
from src.interfaces.proxy_server import PproxyServer
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.standby import StandbyQueueUseCase
from src.use_cases.check_purity import CheckPurityUseCase
from src.framework.web.panel_state import PanelTasks

# pspeed 测速工具（vendor 目录）：sys.path 注册留在模块级（stdlib，零成本），
# 实际 import 下沉到 _run_speedtest——测速是低频面板动作，不为其常驻加载
# requests+PySocks 依赖链（调研实测边际 ~0.1MB/27ms，主要是隔离 vendor 导入面）
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_WEB_DIR)))
_PSPEED_SRC = os.path.join(_PROJECT_ROOT, "vendor", "pspeed", "src")
if _PSPEED_SRC not in sys.path:
    sys.path.insert(0, _PSPEED_SRC)

logger = logging.getLogger(__name__)


# aiohttp 类型化应用 key（避免字符串 key 触发 NotAppKeyWarning）
_PANEL_KEY = web.AppKey("panel", "PanelContext")


@dataclass
class PanelContext:
    """面板运行时上下文：聚合各组件供路由使用（serve 构建并持有唯一实例）"""

    pool: object               # ProxyPool
    fetch: object              # FetchProxiesUseCase
    record: RecordProxyUseCase
    standby: StandbyQueueUseCase
    control: object            # ServeControl
    server: PproxyServer
    save: Callable[[], None]   # 落盘回调（c.save）
    capacity: int              # 代理服务队列容量
    purity: CheckPurityUseCase | None    # IP 纯洁度检查；禁用时为 None
    geo: object | None                   # IpGeoUseCase：IP 归属地图（GeoLite2 + 缓存 + 聚合）；geoip.enabled=false 时为 None
    tasks: PanelTasks                    # 面板任务状态（测速/纯洁度检查进行中与终态，前端刷新后恢复）


# ------------------------------------------------------------------ 读取
def _purity_cached(ctx: PanelContext, proxy: str) -> dict | None:
    """查纯洁度缓存（30 天内有数据则返回 score+usage_type，否则 None）

    在 _status_snapshot 中调用（主线程，SQLite 安全）。只取 tag 需要的字段，
    避免快照 payload 膨胀。纯洁度禁用或 query_cache 异常时不影响整体快照。
    """
    if ctx.purity is None:
        return None
    try:
        cached = ctx.purity.query_cache(proxy)
    except Exception:
        return None
    if cached is None:
        return None
    return {
        "abuse_confidence_score": cached.get("abuse_confidence_score"),
        "usage_type": cached.get("usage_type"),
    }


def _standby_rows(ctx: PanelContext) -> list[str]:
    """面板队列行：锁定代理置首 + 队列成员（去重）

    锁定代理已 move 出队列组，只有 upstream_proxies() 能取到，故并入下发；历史数据
    可能在队列组留有副本，按地址去重避免同一代理出现两行（前端以 proxy 为 :key）。
    """
    rows = list(ctx.standby.standby_proxies(truncate=False))
    if ctx.standby.is_locked():
        locked = ctx.standby.upstream_proxies()
        rows = locked + [p for p in rows if p not in locked]
    return rows


def _geo_payload(geo: dict | None) -> dict | None:
    """IP 归属下发字段（只取展示需要的国家/省市，不带经纬度与缓存时间）"""
    if not geo:
        return None
    return {
        "country": geo["country"],
        "country_code": geo["country_code"],
        "province": geo.get("province"),
        "city": geo.get("city"),
    }


def _status_snapshot(ctx: PanelContext) -> dict:
    """全量状态快照：组统计 / 可用数 / 代理服务队列（含锁定行）/ 待补测 / 源暂停 / 调度参数"""
    pool = ctx.pool
    # 组名 → 中文标签 + 语义类型（从 config 派生，不硬编码）
    _GROUP_META = {
        config.TRUSTED_GROUP: ("信任池", "trusted"),
        config.BLACKLIST_GROUP: ("黑名单", "blacklist"),
        config.STANDBY_GROUP: ("代理队列", "standby"),
        "staging": ("暂存组", "staging"),
    }
    groups = {}
    for name, grp in pool.proxy_groups.items():
        if name == LOCKED_GROUP:  # 隐藏内建锁定组
            continue
        label, kind = _GROUP_META.get(name, (name, "normal"))
        groups[name] = {
            "ttl": grp["ttl"],
            "count": len(grp["proxies"]),
            "active": len(pool.get_active_proxies(name)),
            "label": label,
            "kind": kind,
        }
    available = pool.get_available_proxies()

    # 锁定代理作为 pproxy 唯一上游，物理上已移出队列组；面板把它显示为队列首行
    # （行内标记已锁定、其余行灰显），故并入 standby 下发，省得前端拼接两处数据。
    locked_proxy = ctx.standby.upstream_proxies()[0] if ctx.standby.is_locked() else None

    def _standby_entry(p: str, *, locked: bool) -> dict:
        """队列行：单次 find_proxy 取全部条目字段（joined_at 供前端算入队时长）

        不下发 source_group（move_proxy 不保留该字段，仅黑名单条目记录来源组，队列行的值恒为
        None）与 protocol（前端不展示，scheme 已在 proxy 文本里），发死字段不如不发。
        """
        _, info = pool.find_proxy(p)
        info = info or {}
        return {
            "proxy": p,
            "latency": info.get("latency"),
            "consecutive_fails": info.get("consecutive_fails", 0),
            "pinned": info.get("pinned", False),
            "purity": _purity_cached(ctx, p),
            # 面板任务状态（前端刷新后据此恢复测速结果/检查中间态；无记录为 None）
            "speedtest": ctx.tasks.speedtest_of(bind_of(p)),
            "purity_state": ctx.tasks.purity_of(proxy_to_ip(p)),
            "locked": locked,
            "joined_at": info.get("joined_at"),
        }

    standby = [_standby_entry(p, locked=(p == locked_proxy)) for p in _standby_rows(ctx)]
    traffic_upload, traffic_download = ctx.server.traffic_total()
    sources = []
    for s in config.SOURCES:
        sources.append({
            "name": s["name"],
            "short_name": s["name"].rsplit(".", 1)[-1],
            "enabled": s["enabled"],
            "paused": pool.is_source_paused(s["name"]),
            "pause_remaining": pool.source_pause_remaining(s["name"]),
        })
    return {
        "groups": groups,
        "available_count": len(available),
        "geo_enabled": ctx.geo is not None,  # 前端据此隐藏/显示 IP 分布地图区块
        "standby": standby,
        "standby_count": len(standby),
        "locked_proxy": locked_proxy,
        "capacity": ctx.capacity,
        "pending_retries": ctx.standby.pending_retries(),
        "sources": sources,
        "settings": ctx.control.to_dict(),
        "traffic_total": {"upload": traffic_upload, "download": traffic_download},
    }


def _status_history(ctx: PanelContext, since: float | None) -> dict:
    """队列指标/流量历史（按需拉取，避免全量下发撑大 /api/status）

    since 指定时只返回 ts > since 的采样点（前端增量追加）；缺省返回留存窗口内全量
    （仪表盘按拿到的真实数据点绘制，不做窗口裁剪）。两份历史同按 METRICS_WINDOW 留存：
    流量体量大（1800s / 2s 采样 ≈ 900 点），队列指标同量级；2 秒轮询 /api/status 均不
    携带，改由前端低频增量拉取。

    retention_window 为服务端**实际生效**的留存时长（秒，取本用例注入的窗口，而非另读
    配置——注入值才是真正约束采样点保留时长的那个，两者一旦不一致，前端按错的值裁剪就
    会丢掉后端仍持有的点）。前端本地缓冲按它裁剪：不硬编码点数上限，因为采样节奏可变
    （心跳 2s，锁定单一上游时 30s），点数上限与留存时长不等价。
    """
    standby_history = [s._asdict() for s in ctx.standby.history() if since is None or s.ts > since]
    traffic_history = [s._asdict() for s in ctx.server.traffic_history() if since is None or s.ts > since]
    traffic_upload, traffic_download = ctx.server.traffic_total()
    return {
        "standby_history": standby_history,
        "traffic_history": traffic_history,
        "retention_window": ctx.standby.metrics_window,
        "traffic_total": {"upload": traffic_upload, "download": traffic_download},
    }


def _standby_series(ctx: PanelContext, since: float | None) -> dict:
    """队列逐代理序列（延时 + 累计流量），供队列页每行内联绘制趋势

    since 指定时只返回 ts > since 的点（前端增量追加），缺省返回留存窗口内全量；
    cursor 为本次下发点的最大时间戳，前端下轮以此为 since；window 为队列页图表的
    显示窗口（= chart_window），前端本地缓冲据此裁剪，不再自己硬编码一份。
    注意：window 只服务队列页行内图；仪表盘走 /api/status/history，按真实数据点绘制。

    - 延时序列的 latency 为 None 表示该轮探测失败，前端据此断开曲线；
    - 流量为累计字节，前端差分算速率；
    - 逐代理全量体量大（队列 30 × 2 序列 × 留存窗口内采样点数），故与 /api/status/history
      同机制走游标增量；代理离队后其序列自然不再出现，前端按现存行裁剪本地缓冲。
    """
    series: dict[str, dict[str, list]] = {}
    cursor = since or 0.0
    for proxy in _standby_rows(ctx):
        bind = bind_of(proxy)
        lat = [[s.ts, s.latency] for s in ctx.standby.per_proxy_latency_history(bind)
               if since is None or s.ts > since]
        # 仅统计有流量的代理（无流量代理的序列为空，前端显示"无流量"）
        traffic = [s for s in ctx.server.per_proxy_traffic_history(bind)
                   if since is None or s.ts > since]
        if not lat and not traffic:
            continue
        entry: dict[str, list] = {}
        if lat:
            entry["lat"] = lat
            cursor = max(cursor, lat[-1][0])
        if traffic:
            entry["up"] = [[s.ts, s.total_upload] for s in traffic]
            entry["down"] = [[s.ts, s.total_download] for s in traffic]
            cursor = max(cursor, traffic[-1].ts)
        series[bind] = entry
    return {"cursor": cursor, "window": ctx.standby.chart_window, "series": series}


async def _group_detail(ctx: PanelContext, name: str) -> dict | None:
    """单组代理明细（地址/加入时间/连续失败数/latency/过期倒计时/来源组/IP 归属）

    IP 归属复用 IpGeoUseCase 三步编排（SQLite 线程绑定）：
    主线程读缓存 → executor 定位缺失 IP（纯 mmdb）→ 主线程写缓存。
    首次打开组页面会补齐该组新增 IP 的归属缓存，之后纯读缓存。
    """
    pool = ctx.pool
    grp = pool.proxy_groups.get(name)
    if grp is None:
        return None
    now = time.time()
    ttl = grp["ttl"]
    proxies = []
    for p, info in grp["proxies"].items():
        expires_in = None if ttl is None else max(0, round(ttl - (now - info["joined_at"])))
        proxies.append({
            "proxy": p,
            "joined_at": info["joined_at"],
            "consecutive_fails": info["consecutive_fails"],
            "latency": info.get("latency"),
            "expires_in": expires_in,
            "source_group": info.get("source_group"),
        })
    # IP 归属：批量查缓存（主线程），缺失的批量定位（executor）+ 写缓存（主线程）
    # geoip.enabled=false 时 geo 为 None，跳过归属查询（proxies 不带 geo 字段）
    geo_map: dict[str, dict] = {}
    if ctx.geo is not None:
        ips = [proxy_to_ip(x["proxy"]) for x in proxies]
        try:
            cached, missing = ctx.geo.query_cache(ips)
            located: dict[str, dict] = {}
            if missing:
                located = await asyncio.get_running_loop().run_in_executor(None, ctx.geo.locate, missing)
                ctx.geo.save_cache(located)
            geo_map = {**cached, **located}
        except Exception:
            logger.exception("代理组 IP 归属查询失败 group=%s", name)
    for x in proxies:
        x["geo"] = _geo_payload(geo_map.get(proxy_to_ip(x["proxy"])))
    return {"group": name, "ttl": ttl, "count": len(proxies), "proxies": proxies}


# ------------------------------------------------------------------ 路由
# static/ 为 Vite 构建产物目录（frontend/ 工程 npm run build 输出，
# emptyOutDir 整体替换）：index.html + /assets/*（文件名带内容 hash）+ world.json。
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_ASSETS_DIR = os.path.join(_STATIC_DIR, "assets")
_WORLD_JSON = os.path.join(_STATIC_DIR, "world.json")

_index_cache: tuple[float, str] | None = None  # (mtime, 内容)：按 mtime 失效，免每请求读盘


def _load_index() -> str:
    """加载 Vite 构建的 index.html（前端页面；数据全部通过 /api/* 获取）"""
    global _index_cache
    path = os.path.join(_STATIC_DIR, "index.html")
    mtime = os.path.getmtime(path)
    if _index_cache is None or _index_cache[0] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            _index_cache = (mtime, f.read())
    return _index_cache[1]


async def _index(request: web.Request) -> web.Response:
    return web.Response(text=_load_index(), content_type="text/html", charset="utf-8")


async def _world_json(request: web.Request) -> web.StreamResponse:
    """世界地图 GeoJSON（ECharts registerMap 数据，约 1MB，前端懒加载一次）"""
    return web.FileResponse(_WORLD_JSON)


async def _logs(request: web.Request) -> web.Response:
    """面板日志增量拉取：?since=<seq> 只返回 seq 之后条目 + 最新游标；缺省返回全量

    缓冲与前端 LogPanel 容量一致（2000，见 logbuffer.py）；面板隐藏时前端不发请求。
    buffer_start = 缓冲最旧条目 seq，前端据此检测 serve 重启/缓冲淘汰导致的
    增量失效（seq 回绕或游标被越过），触发全量重拉。
    """
    since_raw = request.query.get("since", "0")
    try:
        since = int(since_raw)
        if since < 0:
            raise ValueError
    except ValueError:
        raise web.HTTPBadRequest(text="since 必须为非负整数")
    entries, cursor, buffer_start = logbuffer.get_buffer().snapshot(since)
    return web.json_response({"entries": entries, "cursor": cursor, "buffer_start": buffer_start})


async def _logs_clear(request: web.Request) -> web.Response:
    """清空面板日志缓冲（只影响面板镜像，不影响 stderr/文件输出）"""
    logbuffer.get_buffer().clear()
    return web.json_response({"ok": True})


def _since_param(request: web.Request) -> float | None:
    """解析 ?since=<ts> 增量游标（缺省 None 表示返回窗口内全量；非法值 400）"""
    raw = request.query.get("since")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise web.HTTPBadRequest(text="since 必须为数字")


async def _status(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    return web.json_response(_status_snapshot(ctx))


async def _status_history_route(request: web.Request) -> web.Response:
    """队列指标/流量历史：?since=<ts> 增量拉取（只返回 ts > since 的点），缺省返回全量"""
    ctx = request.app[_PANEL_KEY]
    return web.json_response(_status_history(ctx, _since_param(request)))


async def _standby_series_route(request: web.Request) -> web.Response:
    """队列逐代理序列：?since=<ts> 增量拉取（只返回 ts > since 的点），缺省返回全量"""
    ctx = request.app[_PANEL_KEY]
    return web.json_response(_standby_series(ctx, _since_param(request)))


async def _standby_geo(request: web.Request) -> web.Response:
    """代理服务队列的 IP 归属（bind → {country, country_code, province, city}）

    与 _group_detail 同一套三步编排（SQLite 连接线程绑定）：主线程读缓存 → executor
    定位缺失 IP（纯 mmdb）→ 主线程写缓存。归属变化以月计，前端 5 分钟拉一次，不进
    /api/status 的 2s 热路径；geoip 关闭时不注册本路由（前端按 geo_enabled 跳过）。
    """
    ctx = request.app[_PANEL_KEY]
    proxies = _standby_rows(ctx)
    ips = [proxy_to_ip(p) for p in proxies]
    cached: dict[str, dict] = {}
    located: dict[str, dict] = {}
    try:
        cached, missing = ctx.geo.query_cache(ips)
        if missing:
            located = await asyncio.get_running_loop().run_in_executor(None, ctx.geo.locate, missing)
            ctx.geo.save_cache(located)
    except Exception:
        logger.exception("代理服务队列 IP 归属查询失败")
    geo_map = {**cached, **located}
    return web.json_response({
        "geo": {bind_of(p): _geo_payload(geo_map.get(proxy_to_ip(p))) for p in proxies},
    })


async def _group(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    detail = await _group_detail(ctx, request.match_info["name"])
    if detail is None:
        return web.json_response({"error": "组不存在"}, status=404)
    return web.json_response(detail)


async def _do_group_add(request: web.Request) -> web.Response:
    """向指定组批量添加代理"""
    ctx = request.app[_PANEL_KEY]
    group_name = request.match_info["name"]
    if group_name not in ctx.pool.proxy_groups:
        return web.json_response({"error": "组不存在"}, status=404)
    body = await _json_body(request)
    proxies = body.get("proxies", [])
    if not proxies:
        return web.json_response({"error": "缺少 proxies"}, status=400)
    added = 0
    for p in proxies:
        p = normalize_proxy(p.strip())
        if not p:
            continue
        ctx.pool.add_to_group(group_name, p, consecutive_fails=0)
        added += 1
    # 从 standby 组添加时同步 pproxy 上游
    if group_name == config.STANDBY_GROUP:
        ctx.server.sync(ctx.standby.upstream_proxies())
    ctx.save()
    return web.json_response({"ok": True, "added": added})


async def _proxy_history(request: web.Request) -> web.Response:
    """单代理的延时历史 + 流量历史（按需查询，避免全量下发撑大 /api/status）

    查询参数 bind 为代理地址（不含 scheme，如 1.2.3.4:8080），与 per_proxy_traffic 的 key 一致。
    """
    ctx = request.app[_PANEL_KEY]
    bind = request.query.get("bind")
    if not bind:
        return web.json_response({"error": "缺少 bind 参数"}, status=400)
    return web.json_response({
        "bind": bind,
        "latency_history": [s._asdict() for s in ctx.standby.per_proxy_latency_history(bind)],
        "traffic_history": [s._asdict() for s in ctx.server.per_proxy_traffic_history(bind)],
    })


# ------------------------------------------------------------------ 测速
def _find_proxy_by_bind(ctx: PanelContext, bind: str) -> str | None:
    """从面板队列行（含锁定代理）按 bind（地址，不含 scheme）反查完整 proxy URL"""
    return next((p for p in _standby_rows(ctx) if bind_of(p) == bind), None)


def _simplify_speed_result(d: dict | None) -> dict | None:
    """精简 pspeed 返回的 download/upload dict，只保留前端需要的字段"""
    if not d:
        return None
    return {
        "ok": d.get("ok"),
        "best_mbps": d.get("best_mbps"),
        "avg_mbps": d.get("avg_mbps"),
        "bytes": d.get("bytes"),
        "ms": d.get("ms"),
        "node": d.get("node"),
        "err": d.get("err"),
    }


def _run_speedtest(proxy: str) -> dict:
    """同步调 pspeed probe_one：Cloudflare 下载+上传，10s 时限

    在 asyncio.to_thread 中执行，避免阻塞事件循环。pspeed 及其 requests/PySocks
    依赖在此惰性导入（首次测速点击时，~27ms）。
    """
    from pspeed.probe import probe_one
    from pspeed.proxy import parse_one
    from pspeed.nodes import get_download_nodes, get_upload_nodes

    px = parse_one(proxy)
    dl_nodes = get_download_nodes(["cloudflare"])
    ul_nodes = get_upload_nodes(["cloudflare"])
    result = probe_one(
        px, ["download", "upload"],
        dl_nodes=dl_nodes, ul_nodes=ul_nodes,
        dl_bytes=100 * 1024 * 1024,
        ul_bytes=5 * 1024 * 1024,
        multi=1, hard_limit=10.0,
    )
    return {
        "proxy": proxy,
        "download": _simplify_speed_result(result.get("download")),
        "upload": _simplify_speed_result(result.get("upload")),
    }


async def _do_speedtest(request: web.Request) -> web.Response:
    """对指定代理进行 Cloudflare 上传/下载 10s 测速

    请求体 {"bind": "1.2.3.4:8080"}，后端反查完整 proxy URL 后调 pspeed。
    幂等：同一 bind 已在测速中直接返回 {"status": "running"}（前端刷新后恢复
    "测速中"，也防止刷新后重复点击并发起第二个任务）。结果写 PanelTasks，
    /api/status 下发供前端刷新后恢复（服务重启清空，测速为瞬时重操作）。
    """
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    bind = body.get("bind")
    if not bind:
        return web.json_response({"error": "缺少 bind"}, status=400)
    proxy = _find_proxy_by_bind(ctx, bind)
    if not proxy:
        return web.json_response({"error": "未找到该代理"}, status=404)
    # 幂等：进行中直接返回，不重复起任务（await 前置状态，无竞态）
    if not ctx.tasks.speedtest_start(bind):
        return web.json_response({"status": "running"})
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _run_speedtest, proxy)
    except Exception as e:
        logger.exception("测速失败 proxy=%s", proxy)
        ctx.tasks.speedtest_error(bind, str(e))
        return web.json_response({"error": f"测速失败: {e}"}, status=500)
    ctx.tasks.speedtest_done(bind, result)
    return web.json_response(result)


async def _do_purity(request: web.Request) -> web.Response:
    """检查代理纯洁度：调多源 API（AbuseIPDB + ipok.io）查询 IP 滥用置信度与 usageType

    请求体 {"bind": "1.2.3.4:8080"}，后端反查完整 proxy URL 后调 CheckPurityUseCase。
    多源分数取最高，usage_type 用最高分源的。

    线程安全：SQLite 连接线程绑定，故分三步编排——
      1. query_cache 在事件循环线程（主线程 SQLite 读）
      2. check_remote 在 executor 线程（纯网络，不碰 SQLite）
      3. save_cache 在事件循环线程（主线程 SQLite 写）
    """
    ctx = request.app[_PANEL_KEY]
    if ctx.purity is None:
        return web.json_response({"error": "纯洁度检查未启用"}, status=404)
    body = await _json_body(request)
    bind = body.get("bind")
    if not bind:
        return web.json_response({"error": "缺少 bind"}, status=400)
    proxy = _find_proxy_by_bind(ctx, bind)
    if not proxy:
        return web.json_response({"error": "未找到该代理"}, status=404)
    # 1. 查缓存（主线程 SQLite 读）
    cached = ctx.purity.query_cache(proxy)
    if cached is not None:
        return web.json_response(cached)
    # 幂等键用 IP（同 IP 多端口行共享 checking，防重复调 API 日限额）：
    # 已在检查中直接返回 {"status": "checking"}（await 前置状态，无竞态）
    ip = proxy_to_ip(proxy)
    if not ctx.tasks.purity_start(ip):
        return web.json_response({"status": "checking"})
    try:
        # 2. 调多源 API（executor 线程，纯网络）
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, ctx.purity.check_remote, proxy)
        except Exception as e:
            logger.exception("纯洁度检查失败 proxy=%s", proxy)
            ctx.tasks.purity_error(ip, str(e))
            return web.json_response({"error": f"检查失败: {e}"}, status=500)
        # 3. 写缓存（主线程 SQLite 写）
        final = ctx.purity.save_cache(proxy, result)
    except Exception as e:
        logger.exception("纯洁度结果写缓存失败 proxy=%s", proxy)
        ctx.tasks.purity_error(ip, str(e))
        return web.json_response({"error": f"写缓存失败: {e}"}, status=500)
    # 清除中间态（结果由 SQLite 缓存承载，快照读 row.purity）
    ctx.tasks.purity_done(ip)
    return web.json_response(final)


async def _json_body(request: web.Request) -> dict:
    """读取请求 JSON 体（缺失/非法返回空 dict，由具体动作校验）"""
    try:
        return await request.json()
    except Exception:
        return {}


async def _geo_map(request: web.Request) -> web.Response:
    """IP 归属地图：池中全部 IP 定位后聚合为城市级散点 + 国家级汇总

    线程安全（SQLite 连接线程绑定）三步编排：
      1. collect_ips + query_cache 在事件循环线程（主线程 SQLite 读）
      2. locate 在 executor 线程（纯 mmdb 磁盘读，不碰 SQLite）
      3. save_cache 在事件循环线程（主线程 SQLite 写）
    首次请求全量定位并落缓存，之后纯读缓存（毫秒级）。
    """
    ctx = request.app[_PANEL_KEY]
    # 1. 收集池中全部 IP + 查缓存（主线程）
    ips = ctx.geo.collect_ips(ctx.pool)
    cached, missing = ctx.geo.query_cache(ips)
    # 2. 缺失 IP 批量定位（executor 线程，纯 mmdb 查询）
    located: dict[str, dict] = {}
    if missing:
        try:
            located = await asyncio.get_running_loop().run_in_executor(None, ctx.geo.locate, missing)
        except Exception:
            logger.exception("IP 归属批量定位失败: %d 个", len(missing))
            located = {}
        # 3. 写缓存（主线程 SQLite 写）
        ctx.geo.save_cache(located)
    # 4. 聚合（纯计算）
    geo_map = {**cached, **located}
    agg = ctx.geo.aggregate(geo_map)
    return web.json_response({
        "total_ips": len(ips),
        "resolved_ips": len(geo_map),
        "unresolved_ips": len(ips) - len(geo_map),
        "countries": agg["countries"],
        "points": agg["points"],
    })


async def _do_fetch(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    await asyncio.get_running_loop().run_in_executor(None, ctx.fetch.execute)   # 大量网络请求，避免阻塞事件循环
    ctx.save()
    return web.json_response({"ok": True, "available": len(ctx.pool.get_available_proxies())})


async def _do_blacklist(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxies = body.get("proxies", [])
    for p in proxies:
        ctx.record.remove(normalize_proxy(p))
    # 若有代理从 standby 队列移出，同步 pproxy 上游
    ctx.server.sync(ctx.standby.upstream_proxies())
    ctx.save()
    return web.json_response({"ok": True, "count": len(proxies)})


async def _do_proxy_move(request: web.Request) -> web.Response:
    """将代理移到指定组（批量：proxies 数组，从 standby 队列移出时同步 pproxy 上游）"""
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxies = body.get("proxies")
    target = body.get("target_group")
    if not proxies or not target:
        return web.json_response({"error": "缺少 proxies 或 target_group"}, status=400)
    if target not in ctx.pool.proxy_groups:
        return web.json_response({"error": f"组 {target} 不存在"}, status=400)
    moved = []
    for p in proxies:
        p = normalize_proxy(p)
        ctx.pool.move_proxy(p, target)
        moved.append(p)
    # 从 standby 队列移出时同步 pproxy 上游
    ctx.server.sync(ctx.standby.upstream_proxies())
    ctx.save()
    return web.json_response({"ok": True, "count": len(moved), "proxies": moved, "target_group": target})


async def _do_proxy_remove(request: web.Request) -> web.Response:
    """从指定组删除代理（批量：proxies 数组，不拉黑，直接移除）"""
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxies = body.get("proxies")
    group = body.get("group")
    if not proxies or not group:
        return web.json_response({"error": "缺少 proxies 或 group"}, status=400)
    removed = []
    for p in proxies:
        p = normalize_proxy(p)
        if ctx.pool.remove_from_group(group, p):
            removed.append(p)
    if not removed:
        return web.json_response({"error": "代理不在该组中"}, status=404)
    # 从 standby 队列移除时同步 pproxy 上游
    if group == config.STANDBY_GROUP:
        ctx.server.sync(ctx.standby.upstream_proxies())
    ctx.save()
    return web.json_response({"ok": True, "count": len(removed), "proxies": removed, "group": group})


async def _do_trust(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxies = body.get("proxies", [])
    for p in proxies:
        ctx.record.trust(normalize_proxy(p))
    ctx.save()
    return web.json_response({"ok": True, "count": len(proxies)})


async def _do_source_pause(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    src = body.get("source")
    if not src:
        return web.json_response({"error": "缺少 source"}, status=400)
    ctx.pool.pause_source(src, ttl=body.get("ttl"))
    ctx.save()
    return web.json_response({"ok": True, "pause_remaining": ctx.pool.source_pause_remaining(src)})


async def _do_source_resume(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    src = body.get("source")
    if not src:
        return web.json_response({"error": "缺少 source"}, status=400)
    ctx.pool.source_paused.pop(src, None)
    ctx.save()
    return web.json_response({"ok": True, "resumed": True})


async def _do_standby_pin(request: web.Request) -> web.Response:
    """固定代理服务队列中的代理：排到队列最前，不被踢出/拉黑"""
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxy = body.get("proxy")
    if not proxy:
        return web.json_response({"error": "缺少 proxy"}, status=400)
    proxy = normalize_proxy(proxy)
    group, _ = ctx.pool.find_proxy(proxy)
    if group != ctx.standby.standby_group:
        return web.json_response({"error": "代理不在代理服务队列"}, status=400)
    ctx.pool.pin(proxy)
    ctx.save()
    return web.json_response({"ok": True, "pinned": True})


async def _do_standby_unpin(request: web.Request) -> web.Response:
    """解除固定"""
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxy = body.get("proxy")
    if not proxy:
        return web.json_response({"error": "缺少 proxy"}, status=400)
    proxy = normalize_proxy(proxy)
    ctx.pool.unpin(proxy)
    ctx.save()
    return web.json_response({"ok": True, "pinned": False})


async def _do_standby_lock(request: web.Request) -> web.Response:
    """锁定代理为 pproxy 唯一上游：move 到 LOCKED_GROUP，sync pproxy 上游

    锁定后无论心跳是否成功都只用该代理，队列其他代理保留不动，解锁后恢复。
    """
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    proxy = body.get("proxy")
    if not proxy:
        return web.json_response({"error": "缺少 proxy"}, status=400)
    proxy = normalize_proxy(proxy)
    group, _ = ctx.pool.find_proxy(proxy)
    if group != ctx.standby.standby_group:
        return web.json_response({"error": "代理不在代理服务队列"}, status=400)
    ctx.standby.lock(proxy)
    ctx.server.sync(ctx.standby.upstream_proxies())  # 同步 pproxy 上游为单一代理
    ctx.save()
    return web.json_response({"ok": True, "locked": True, "proxy": proxy})


async def _do_standby_unlock(request: web.Request) -> web.Response:
    """解除锁定：代理 move 回代理服务队列，sync pproxy 上游恢复"""
    ctx = request.app[_PANEL_KEY]
    unlocked = ctx.standby.unlock()
    if unlocked:
        ctx.server.sync(ctx.standby.upstream_proxies())
    ctx.save()
    return web.json_response({"ok": True, "locked": False, "proxy": unlocked})


async def _do_settings(request: web.Request) -> web.Response:
    """更新调度策略参数；落地副作用（salgorithm 重启 / fill 倍率同步）"""
    ctx = request.app[_PANEL_KEY]
    ctl = ctx.control
    body = await _json_body(request)
    try:
        changed = ctl.update(body)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if "salgorithm" in changed:
        await ctx.server.set_salgorithm(ctl.salgorithm)
    if "fill_probe_multiplier" in changed or "fill_probe_min_extra" in changed:
        ctx.standby.update_fill_probe_params(ctl.fill_probe_multiplier, ctl.fill_probe_min_extra)
    ctx.save()
    return web.json_response({"ok": True, "changed": changed, "settings": ctl.to_dict()})


@web.middleware
async def _static_cache_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """静态缓存策略：index.html 禁缓存（入口即时生效）；/assets/* 文件名带内容
    hash，可永久缓存（immutable），无需每次协商。"""
    response = await handler(request)
    if request.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def build_app(ctx: PanelContext) -> web.Application:
    """构造 aiohttp 应用并注册路由（serve 通过 WebPanel 挂载到事件循环）

    前端为 Vite 构建产物：/ 返回 index.html，/assets/* 为带 hash 的 JS/CSS，
    /world.json 为地图数据；API 路由与前端解耦，契约见 frontend/src/types/api.ts。
    """
    app = web.Application(middlewares=[_static_cache_middleware])
    app[_PANEL_KEY] = ctx
    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/status/history", _status_history_route)
    app.router.add_get("/api/standby/series", _standby_series_route)
    app.router.add_get("/api/logs", _logs)
    app.router.add_post("/api/logs/clear", _logs_clear)
    app.router.add_get("/api/group/{name}", _group)
    app.router.add_post("/api/group/{name}/add", _do_group_add)
    app.router.add_get("/api/proxy/history", _proxy_history)
    app.router.add_post("/api/proxy/speedtest", _do_speedtest)
    app.router.add_post("/api/proxy/purity", _do_purity)
    # geoip.enabled=false 时不注册地图/归属路由（前端按 /api/status 的 geo_enabled 跳过）
    if ctx.geo is not None:
        app.router.add_get("/api/geo/map", _geo_map)
        app.router.add_get("/api/standby/geo", _standby_geo)
    app.router.add_post("/api/fetch", _do_fetch)
    app.router.add_post("/api/blacklist", _do_blacklist)
    app.router.add_post("/api/proxy/move", _do_proxy_move)
    app.router.add_post("/api/proxy/remove", _do_proxy_remove)
    app.router.add_post("/api/trust", _do_trust)
    app.router.add_post("/api/source/pause", _do_source_pause)
    app.router.add_post("/api/source/resume", _do_source_resume)
    app.router.add_post("/api/standby/pin", _do_standby_pin)
    app.router.add_post("/api/standby/unpin", _do_standby_unpin)
    app.router.add_post("/api/standby/lock", _do_standby_lock)
    app.router.add_post("/api/standby/unlock", _do_standby_unlock)
    app.router.add_post("/api/settings", _do_settings)
    app.router.add_get("/world.json", _world_json)
    app.router.add_static("/assets", _ASSETS_DIR, name="assets")
    return app
