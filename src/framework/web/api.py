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

from src.domain.models import bind_of, normalize_proxy
from src.domain.proxy_pool import LOCKED_GROUP
from src.framework import config
from src.interfaces.proxy_server import PproxyServer
from src.use_cases.record_proxy import RecordProxyUseCase
from src.use_cases.standby import StandbyQueueUseCase

# pspeed 测速工具（vendor 目录，加入 sys.path 后 import）
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_WEB_DIR)))
_PSPEED_SRC = os.path.join(_PROJECT_ROOT, "vendor", "pspeed", "src")
if _PSPEED_SRC not in sys.path:
    sys.path.insert(0, _PSPEED_SRC)
from pspeed.probe import probe_one as _pspeed_probe
from pspeed.proxy import parse_one as _pspeed_parse
from pspeed.nodes import get_download_nodes as _pspeed_dl_nodes, get_upload_nodes as _pspeed_ul_nodes

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


# ------------------------------------------------------------------ 读取
def _status_snapshot(ctx: PanelContext) -> dict:
    """全量状态快照：组统计 / 可用数 / 代理服务队列 / 待补测 / 源暂停 / 调度参数"""
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
    standby = [
        {
            "proxy": p,
            "latency": pool.get_latency(p),
            "consecutive_fails": pool.get_consecutive_fail_count(p),
            "pinned": pool.is_pinned(p),
        }
        for p in ctx.standby.standby_proxies(truncate=False)
    ]
    # 锁定状态：locked 代理作为 pproxy 唯一上游（无论心跳是否成功）
    locked_proxy = None
    if ctx.standby.is_locked():
        locked_list = ctx.standby.upstream_proxies()
        if locked_list:
            locked_proxy = locked_list[0]
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
        "standby": standby,
        "standby_count": len(standby),
        "locked_proxy": locked_proxy,
        "capacity": ctx.capacity,
        "pending_retries": ctx.standby.pending_retries(),
        "sources": sources,
        "settings": ctx.control.to_dict(),
        "standby_history": [s._asdict() for s in ctx.standby.history()],
        "traffic_history": [s._asdict() for s in ctx.server.traffic_history()],
        "traffic_total": {"upload": traffic_upload, "download": traffic_download},
        "per_proxy_traffic": ctx.server.per_proxy_traffic(),
    }


def _group_detail(ctx: PanelContext, name: str) -> dict | None:
    """单组代理明细（地址/加入时间/连续失败数/latency/过期倒计时/来源组）"""
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
    return {"group": name, "ttl": ttl, "count": len(proxies), "proxies": proxies}


# ------------------------------------------------------------------ 路由
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _load_index() -> str:
    """加载静态 index.html（前端页面；数据全部通过 /api/* 获取）"""
    path = os.path.join(_STATIC_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as _f:
        return _f.read()


async def _index(request: web.Request) -> web.Response:
    return web.Response(text=_load_index(), content_type="text/html", charset="utf-8")


async def _status(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    return web.json_response(_status_snapshot(ctx))


async def _group(request: web.Request) -> web.Response:
    ctx = request.app[_PANEL_KEY]
    detail = _group_detail(ctx, request.match_info["name"])
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
    """从 standby 队列 + 锁定代理中按 bind（地址，不含 scheme）反查完整 proxy URL"""
    for p in ctx.standby.standby_proxies(truncate=False):
        if bind_of(p) == bind:
            return p
    if ctx.standby.is_locked():
        for p in ctx.standby.upstream_proxies():
            if bind_of(p) == bind:
                return p
    return None


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

    在 asyncio.to_thread 中执行，避免阻塞事件循环。
    """
    px = _pspeed_parse(proxy)
    dl_nodes = _pspeed_dl_nodes(["cloudflare"])
    ul_nodes = _pspeed_ul_nodes(["cloudflare"])
    result = _pspeed_probe(
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
    """
    ctx = request.app[_PANEL_KEY]
    body = await _json_body(request)
    bind = body.get("bind")
    if not bind:
        return web.json_response({"error": "缺少 bind"}, status=400)
    proxy = _find_proxy_by_bind(ctx, bind)
    if not proxy:
        return web.json_response({"error": "未找到该代理"}, status=404)
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _run_speedtest, proxy)
    except Exception as e:
        logger.exception("测速失败 proxy=%s", proxy)
        return web.json_response({"error": f"测速失败: {e}"}, status=500)
    return web.json_response(result)


async def _json_body(request: web.Request) -> dict:
    """读取请求 JSON 体（缺失/非法返回空 dict，由具体动作校验）"""
    try:
        return await request.json()
    except Exception:
        return {}


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
async def _no_cache_static_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """静态资源中间件：禁用缓存，避免前端更新后浏览器仍使用旧文件。"""
    response = await handler(request)
    if request.path.startswith("/static") or request.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def build_app(ctx: PanelContext) -> web.Application:
    """构造 aiohttp 应用并注册路由（serve 通过 WebPanel 挂载到事件循环）

    除 API 路由外，注册 /static 静态文件服务以支持拆分后的 CSS/JS/图标资源；
    通过中间件禁用静态资源缓存，保证前端更新即时生效。
    """
    app = web.Application(middlewares=[_no_cache_static_middleware])
    app[_PANEL_KEY] = ctx
    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/group/{name}", _group)
    app.router.add_post("/api/group/{name}/add", _do_group_add)
    app.router.add_get("/api/proxy/history", _proxy_history)
    app.router.add_post("/api/proxy/speedtest", _do_speedtest)
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
    app.router.add_static("/static", _STATIC_DIR, name="static")
    return app
