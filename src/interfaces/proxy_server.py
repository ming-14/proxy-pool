"""pproxy 本地代理服务器封装（interfaces 层）

将隐藏池（代理队列）同步为 pproxy 的上游 rserver 列表，实现本地 HTTP/SOCKS4/SOCKS5
正向代理服务器。rserver 是同一列表引用，运行中增删对 schedule 实时生效，
配合 pproxy 的 rc（random）调度——每次请求随机选存活代理，坏代理有概率被跳过，
避免 fa（first_available）固定命中第一个坏代理导致持续失败（见 __init__），实现无缝切换。

**绝不直连**：无可用上游时按协议回错误（HTTP 502 / SOCKS5 REP 0x01 / SOCKS4 CD 0x5B），
不退回以本机真实 IP 直连目标（见 _handler_args 的 direct_fallback=False）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from typing import Iterable, NamedTuple

# pproxy 已 vendor 化（vendor/pproxy/），本地维护，修复已内化进源码，无需 monkey-patch：
#   - proto.sslwrap：verbose=None 时静默（原退化为 print 刷 stdout）
#   - server.sslcontexts：弱引用容器（原全局 list 只 append 不清理 → SSLContext 泄漏）
#   - server.stream_handler：modstat 钩子签名加 roption（原不传选中上游 → 无法按代理统计流量）
# 其余协议层修复（SOCKS5 认证应答/UDP ASSOCIATE、HTTP 404、WebSocket 握手、
# 通道空闲回收改会话双向静默——修复流式长连接被单向 60s 读超时误杀 等）见 docs/filestree.md。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

import pproxy

from src.interfaces.proxy_scheme import engine_uri

logger = logging.getLogger(__name__)


class TrafficSample(NamedTuple):
    """流量采样点（累计字节），与 StandbySample 同机制：NamedTuple 紧凑存储 + deque 按时间窗口清理"""
    ts: float
    total_upload: int       # 累计上行字节（客户端→远程）
    total_download: int     # 累计下行字节（远程→客户端）


class PproxyServer:
    """pproxy 本地代理服务器封装：管理上游 rserver 的动态同步"""

    def __init__(self, listen_uri: str, *, salgorithm: str = "rc", metrics_window: float = 1800) -> None:
        """
        Args:
            listen_uri: 本地监听 URI，如 "http+socks4+socks5://127.0.0.1:8080"
            salgorithm: 上游调度算法（fa=first_available, rr=轮询, rc=随机, lc=最少连接）。
                        默认 rc（随机）：每次请求随机选存活代理，坏代理有概率被跳过，
                        避免 fa 固定用第一个坏代理导致持续失败。
            metrics_window: 采样数据留存时长秒（全局），流量采样点按此清理，
                           与队列指标共用同一留存窗口
        """
        self._listen_uri = listen_uri
        self._salgorithm = salgorithm
        self._metrics_window = metrics_window
        self._server = pproxy.Server(listen_uri)
        self._handler = None
        self._rserver: list = []
        # 是否处于"无可用上游"状态（列表为空或成员全部被标记失败，见 _log_upstream_change）
        self._no_upstream = False
        # uri → Connection 索引：sync 按完整 uri 匹配（pproxy.Connection.bind 只存地址不含
        # scheme 前缀，直接用 bind 比较会导致 http+ssl:// 上游每轮全量增删 → SSLContext 雪崩泄漏）
        self._uri_index: dict[str, object] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._traffic_upload = 0       # 累计上行字节
        self._traffic_download = 0     # 累计下行字节
        self._traffic_history: deque[TrafficSample] = deque()
        # 按上游代理地址（roption.bind）的累计上下行字节；显式 direct:// 上游的 bind 为 "DIRECT"
        self._per_proxy_upload: dict[str, int] = {}
        self._per_proxy_download: dict[str, int] = {}
        # 按上游代理地址的流量采样历史（与 _traffic_history 同机制），供按 IP 查流量趋势
        self._per_proxy_traffic_history: dict[str, deque[TrafficSample]] = {}

    async def start(self, proxies: list[str]) -> None:
        """启动服务器，用给定代理初始化上游列表"""
        self._rserver = [pproxy.Connection(engine_uri(p)) for p in proxies]
        self._uri_index = {engine_uri(p): conn for p, conn in zip(proxies, self._rserver)}
        self._loop = asyncio.get_running_loop()
        self._handler = await self._server.start_server(self._handler_args())
        self._no_upstream = not self._rserver
        logger.info("代理服务器已启动: %s, 上游 %d 个代理, 调度 %s", self._listen_uri, len(self._rserver), self._salgorithm)

    def _handler_args(self) -> dict:
        """pproxy stream_handler 参数（start 与 set_salgorithm 重建 handler 共用一份）

        键名必须与 pproxy stream_handler 签名一致，否则被 **kwargs 静默吞掉：
        `salgorithm`（传 s= 会恒为默认 fa）、`modstat`（不传则无流量统计）、
        `direct_fallback`。

        direct_fallback=False 是本项目的硬约束：**无可用上游时按协议回错误，
        绝不以本机真实 IP 直连目标**（直连会让"只为换出口 IP 而用代理"的场景
        静默泄漏真实 IP，客户端拿到的是成功响应，无从察觉）。需要直连的场景
        必须显式把 direct:// 放进上游列表。
        """
        return dict(rserver=self._rserver, verbose=lambda *a: None,
                    salgorithm=self._salgorithm, modstat=self._make_modstat(),
                    direct_fallback=False)

    def _log_upstream_change(self) -> None:
        """无可用上游 ↔ 恢复 的状态翻转日志（_apply_health 每次调用，只在翻转时输出）

        无可用上游（列表为空、或成员全部被标记为失败）时所有请求都被拒绝（见
        _handler_args），不记日志的话运维只能看到"客户端全部 502"而不知原因。
        """
        alive = sum(1 for conn in self._rserver if conn.alive)
        no_upstream = alive == 0
        if no_upstream == self._no_upstream:
            return
        self._no_upstream = no_upstream
        if no_upstream:
            detail = f"{len(self._rserver)} 个上游全部不可用" if self._rserver else "上游列表为空"
            logger.warning("无可用上游（%s）：请求将被拒绝（不回退直连，避免泄漏本机 IP）", detail)
        else:
            logger.info("上游可用: %d/%d 个代理", alive, len(self._rserver))

    def sync(self, proxies: list[str], unhealthy: Iterable[str] = ()) -> None:
        """将 rserver 同步到给定代理列表（运行中增删，schedule 实时可见）+ 写入存活标记

        用完整 pproxy uri 作匹配键（_uri_index），而非 Connection.bind——bind 只存地址
        不含 scheme 前缀，对 http+ssl:// 上游会与 target（含前缀）永远对不上，导致每轮
        全量移除+全量新建，触发 pproxy SSLContext 雪崩式创建 → 内存泄漏/堆损坏崩溃。

        unhealthy 是代理池判定"当前不可用"的成员（正在退避补测），对应上游对象置
        alive=False：pproxy 的 schedule 按 alive 过滤（vendor server.py 的 filter_cond），
        于是 rc/rr/lc 只在存活代理里挑，不会把请求交给已知死掉的代理；全部不可用时
        schedule 返回 None → 按 no-direct 回错误（见 _handler_args）。pproxy 自带的
        check_server_alive 只在 CLI（-a）里启动、本项目不用它：存活判定唯一来源是
        代理池心跳（避免重复探测，且"TCP 能连"≠代理可用）。

        需在服务器启动后、事件循环同线程调用。
        """
        if self._handler is None:
            raise RuntimeError("服务器未启动")
        target = {engine_uri(p) for p in proxies}

        # 移除已不在目标中的上游（按 uri 匹配，非 bind）
        for uri in list(self._uri_index):
            if uri not in target:
                conn = self._uri_index.pop(uri)
                self._rserver.remove(conn)
        # 追加新增的上游
        for uri in sorted(target - self._uri_index.keys()):
            conn = pproxy.Connection(uri)
            self._uri_index[uri] = conn
            self._rserver.append(conn)

        logger.debug("上游同步: %d 个代理", len(self._rserver))
        self._apply_health(unhealthy)

    def _apply_health(self, unhealthy: Iterable[str]) -> None:
        """把代理池的存活判定写进 pproxy 上游对象的 alive（schedule 的过滤条件）

        未被标记的自动回到 alive（补测成功 / 新入队即恢复）；标记项不在上游列表中
        （已拉黑移出等）自然忽略。Connection 对象按 uri 复用，故标记在 sync 间保持。
        """
        dead = {engine_uri(p) for p in unhealthy}
        for uri, conn in self._uri_index.items():
            alive = uri not in dead
            if conn.alive != alive:
                logger.debug("上游存活标记变更: %s -> %s", uri, "alive" if alive else "dead")
                conn.alive = alive
        self._log_upstream_change()

    async def set_salgorithm(self, salgorithm: str) -> None:
        """运行时切换上游调度算法（Web 面板调整；重启 rserver 生效，保留现有上游列表）"""
        if salgorithm == self._salgorithm:
            return
        self._salgorithm = salgorithm
        if self._handler is not None:
            self._handler.close()
            await self._handler.wait_closed()
            self._handler = await self._server.start_server(self._handler_args())
            logger.info("调度算法已切换为 %s, 上游 %d 个代理", self._salgorithm, len(self._rserver))

    async def close(self) -> None:
        """关闭服务器"""
        if self._handler is not None:
            self._handler.close()
            await self._handler.wait_closed()
            self._handler = None
            logger.info("代理服务器已关闭")

    # ------------------------------------------------------------------ 流量统计
    def _make_modstat(self):
        """构造 pproxy modstat 钩子：按方向 i 分发到上行/下行计数器

        pproxy stream_handler 选定上游 roption 后调 modstat(user, remote_ip, host_name, roption)
       （vendor/pproxy 已内化该签名，原版不传 roption 导致无法按代理统计流量）。
        pproto.channel 转发数据时调 stat_bytes(len(data))：
          i ∈ {0,1}（roption.direct=0 代理 / 1 直连）→ 上行（客户端→远程）
          i ∈ {2,3} → 下行（远程→客户端）
          i ∈ {4,5} → 连接数变化（不统计）

        per-proxy 维度：roption.bind（上游代理地址，直连上游为 "DIRECT"）作 key 分发到 per-proxy 计数器。
        pproxy 选定上游后才会调 modstat（无上游直接回错误，不进入统计），故 roption 恒非 None。
        """
        def modstat(user, remote_ip, host_name, roption):
            bind = roption.bind
            def m(i):
                if i in (0, 1):
                    return lambda n: self._add_upload(bind, n)
                if i in (2, 3):
                    return lambda n: self._add_download(bind, n)
                return lambda n: None
            return m
        return modstat

    def _add_upload(self, bind: str, n: int) -> None:
        self._traffic_upload += n
        self._per_proxy_upload[bind] = self._per_proxy_upload.get(bind, 0) + n

    def _add_download(self, bind: str, n: int) -> None:
        self._traffic_download += n
        self._per_proxy_download[bind] = self._per_proxy_download.get(bind, 0) + n

    def sample_traffic(self) -> None:
        """采当前累计流量到历史并按时间窗口清理过期点（与 StandbyQueueUseCase._sample 同机制）

        同时为每个有流量的上游代理（bind）采一笔 per-proxy 历史，供按 IP 查流量趋势。
        """
        now = time.time()
        cutoff = now - self._metrics_window
        # 全局
        self._traffic_history.append(TrafficSample(now, self._traffic_upload, self._traffic_download))
        while self._traffic_history and self._traffic_history[0].ts < cutoff:
            self._traffic_history.popleft()
        # per-proxy：为每个有流量的 bind 各采一笔
        for bind in set(self._per_proxy_upload) | set(self._per_proxy_download):
            dq = self._per_proxy_traffic_history.get(bind)
            if dq is None:
                dq = deque()
                self._per_proxy_traffic_history[bind] = dq
            dq.append(TrafficSample(now, self._per_proxy_upload.get(bind, 0),
                                    self._per_proxy_download.get(bind, 0)))
            while dq and dq[0].ts < cutoff:
                dq.popleft()

    def traffic_history(self) -> list[TrafficSample]:
        """流量采样历史（按时间正序），供 /api/status 下发前端图表重绘"""
        return list(self._traffic_history)

    def traffic_total(self) -> tuple[int, int]:
        """当前累计上行/下行字节"""
        return self._traffic_upload, self._traffic_download

    def per_proxy_traffic(self) -> dict[str, dict[str, int]]:
        """按上游代理地址（bind）的累计上下行字节，供 /api/status 下发前端按 IP 展示流量

        key 为 roption.bind（上游代理地址如 "1.2.3.4:8080"；显式 direct:// 上游为 "DIRECT"），
        value 为 {"upload": int, "download": int}。仅内存实时计数，重启清零。
        """
        binds = set(self._per_proxy_upload) | set(self._per_proxy_download)
        return {bind: {
            "upload": self._per_proxy_upload.get(bind, 0),
            "download": self._per_proxy_download.get(bind, 0),
        } for bind in binds}

    def per_proxy_traffic_history(self, bind: str) -> list[TrafficSample]:
        """指定上游代理地址的流量采样历史（按时间正序），供 /api/proxy/history 按需查询

        与 traffic_history() 同机制：累计字节序列，前端差分算速率。仅内存，重启清零。
        """
        return list(self._per_proxy_traffic_history.get(bind, ()))
