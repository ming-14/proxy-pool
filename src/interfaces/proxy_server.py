"""pproxy 本地代理服务器封装（interfaces 层）

将隐藏池（代理队列）同步为 pproxy 的上游 rserver 列表，实现本地 HTTP/SOCKS4/SOCKS5
正向代理服务器。rserver 是同一列表引用，运行中增删对 schedule 实时生效，
配合 pproxy 的 rc（random）调度——每次请求随机选存活代理，坏代理有概率被跳过，
避免 fa（first_available）固定命中第一个坏代理导致持续失败（见 __init__），实现无缝切换。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from typing import NamedTuple

# pproxy 已 vendor 化（vendor/pproxy/），本地维护。三处修复已内化进源码，无需 monkey-patch：
#   - proto.sslwrap：verbose=None 时静默（原退化为 print 刷 stdout）
#   - server.sslcontexts：弱引用容器（原全局 list 只 append 不清理 → SSLContext 泄漏）
#   - server.stream_handler：modstat 钩子签名加 roption（原不传选中上游 → 无法按代理统计流量）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

import pproxy

logger = logging.getLogger(__name__)


class TrafficSample(NamedTuple):
    """流量采样点（累计字节），与 StandbySample 同机制：NamedTuple 紧凑存储 + deque 按时间窗口清理"""
    ts: float
    total_upload: int       # 累计上行字节（客户端→远程）
    total_download: int     # 累计下行字节（远程→客户端）


def _pproxy_uri(proxy: str) -> str:
    """将池内代理字符串转换为 pproxy 上游 URI

    http:// 与 socks5:// 直接可用；https:// 是 HTTPS 代理（代理本身走 TLS），
    pproxy 用 http+ssl:// 前缀表示。
    """
    if proxy.startswith("https://"):
        # pproxy 用 http+ssl:// 表示 HTTPS 代理上游（TLS 到代理）
        return f"http+ssl://{proxy[len('https://'):]}"
    return proxy


class PproxyServer:
    """pproxy 本地代理服务器封装：管理上游 rserver 的动态同步"""

    def __init__(self, listen_uri: str, *, salgorithm: str = "rc", metrics_window: float = 1800) -> None:
        """
        Args:
            listen_uri: 本地监听 URI，如 "http+socks4+socks5://127.0.0.1:8080"
            salgorithm: 上游调度算法（fa=first_available, rr=轮询, rc=随机, lc=最少连接）。
                        默认 rc（随机）：每次请求随机选存活代理，坏代理有概率被跳过，
                        避免 fa 固定用第一个坏代理导致持续失败。
            metrics_window: 流量采样存储时长秒（窗口），与 standby 指标共用同一窗口
        """
        self._listen_uri = listen_uri
        self._salgorithm = salgorithm
        self._metrics_window = metrics_window
        self._server = pproxy.Server(listen_uri)
        self._handler = None
        self._rserver: list = []
        # uri → Connection 索引：sync 按完整 uri 匹配（pproxy.Connection.bind 只存地址不含
        # scheme 前缀，直接用 bind 比较会导致 http+ssl:// 上游每轮全量增删 → SSLContext 雪崩泄漏）
        self._uri_index: dict[str, object] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._traffic_upload = 0       # 累计上行字节
        self._traffic_download = 0     # 累计下行字节
        self._traffic_history: deque[TrafficSample] = deque()
        # 按上游代理地址（roption.bind）的累计上下行字节；DIRECT 的 bind 为 "DIRECT"
        self._per_proxy_upload: dict[str, int] = {}
        self._per_proxy_download: dict[str, int] = {}
        # 按上游代理地址的流量采样历史（与 _traffic_history 同机制），供按 IP 查流量趋势
        self._per_proxy_traffic_history: dict[str, deque[TrafficSample]] = {}

    async def start(self, proxies: list[str]) -> None:
        """启动服务器，用给定代理初始化上游列表"""
        self._rserver = [pproxy.Connection(_pproxy_uri(p)) for p in proxies]
        self._uri_index = {_pproxy_uri(p): conn for p, conn in zip(proxies, self._rserver)}
        self._loop = asyncio.get_running_loop()
        args = dict(rserver=self._rserver, verbose=lambda *a: None, s=self._salgorithm,
                    modstat=self._make_modstat())
        self._handler = await self._server.start_server(args)
        logger.info("代理服务器已启动: %s, 上游 %d 个代理, 调度 %s", self._listen_uri, len(self._rserver), self._salgorithm)

    def sync(self, proxies: list[str]) -> None:
        """将 rserver 同步到给定代理列表（运行中增删，schedule 实时可见）

        用完整 pproxy uri 作匹配键（_uri_index），而非 Connection.bind——bind 只存地址
        不含 scheme 前缀，对 http+ssl:// 上游会与 target（含前缀）永远对不上，导致每轮
        全量移除+全量新建，触发 pproxy SSLContext 雪崩式创建 → 内存泄漏/堆损坏崩溃。

        需在服务器启动后、事件循环同线程调用。
        """
        if self._handler is None:
            raise RuntimeError("服务器未启动")
        target = {_pproxy_uri(p) for p in proxies}

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

        logger.info("上游同步: %d 个代理", len(self._rserver))

    async def set_salgorithm(self, salgorithm: str) -> None:
        """运行时切换上游调度算法（Web 面板调整；重启 rserver 生效，保留现有上游列表）"""
        if salgorithm == self._salgorithm:
            return
        self._salgorithm = salgorithm
        if self._handler is not None:
            self._handler.close()
            await self._handler.wait_closed()
            args = dict(rserver=self._rserver, verbose=lambda *a: None, s=self._salgorithm,
                    modstat=self._make_modstat())
            self._handler = await self._server.start_server(args)
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

        per-proxy 维度：roption.bind（上游代理地址，DIRECT 为 "DIRECT"）作 key 分发到 per-proxy 计数器。
        """
        def modstat(user, remote_ip, host_name, roption):
            bind = roption.bind if roption is not None else "DIRECT"
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

        key 为 roption.bind（上游代理地址如 "1.2.3.4:8080"，直连为 "DIRECT"），
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
