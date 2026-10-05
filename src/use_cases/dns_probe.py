"""DNS 探测队列（流量统计的子模块）：事件驱动的穿代理域名解析 + 可选归属定位

流量统计每发现一个 (代理, 域名) 会话就把该 pair 入队，队列**立刻**（有界并发）穿对应
代理做 DoH 解析（HostResolver，DNS 查询从代理出口发出）→ 可选 GeoLite2 定位 → 把结果
行回调给流量统计落盘。队列只负责探测，不碰存储。

模型：
- 入队即干活：常驻消费者 await asyncio.Queue，不轮询不空转；enqueue 是同步 O(1)
  （modstat 热路径调用），在途去重与调度记忆全在内存，不读库。
- 调度记忆 `_schedule`: (下次可探测时刻, 下次失败后的退避秒数)。成功后 = now +
  cache_ttl（TTL 内同一 pair 的新会话直接丢弃，不重复探测）；失败指数退避
  60s→15min，仅当新会话到来且退避已过才重试。
- 跳过：内网/保留地址、单标签主机名、.local 后缀不入队；公网 IP 字面量跳过 DoH
  直接定位。
- geo 为可选依赖：None 时只产出 ip 行（不做经纬度定位）。

并发模型：enqueue / 消费者 / 探测任务全在事件循环单线程内执行；信号量限制
同时在跑的探测数，积压由「取到任务再取信号量」形成天然背压。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Callable, Protocol

from src.domain.ports import HostResolver

logger = logging.getLogger(__name__)

# 失败退避：60s 起指数翻倍，封顶 15min（成功重置为 cache_ttl）
_BACKOFF_INITIAL = 60.0
_BACKOFF_MAX = 900.0
# 每个解析结果最多定位的 A 记录数（多地址只增定位开销，取首个命中即可）
_MAX_GEO_IPS = 2


class GeoResolver(Protocol):
    """IP 归属查询（IpGeoUseCase 实现）：缓存优先，不外抛"""

    async def resolve(self, ips: list[str], *, label: str) -> dict[str, dict]: ...


class DnsProbeQueue:
    """事件驱动的 DNS 探测队列（流量统计持有；结果经 sink 回调交回流量统计落盘）"""

    def __init__(
        self,
        *,
        resolver: HostResolver,
        proxy_lookup: Callable[[str], str | None],
        geo: GeoResolver | None,
        concurrency: int,
        cache_ttl: float,
    ) -> None:
        """
        Args:
            resolver:      穿代理的域名解析器（HostResolver 实现）
            proxy_lookup:  bind → 当前服务队列代理 URL；None 表示该代理已离队（跳过探测）
            geo:           IP 归属查询（None = 不做定位，只产出 ip 行）
            concurrency:   同时在跑的探测数上限
            cache_ttl:     同一 pair 两次探测之间的最小间隔秒（成功后的冷却）
        """
        self._resolver = resolver
        self._proxy_lookup = proxy_lookup
        self._geo = geo
        self._concurrency = concurrency
        self._cache_ttl = cache_ttl
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: set[tuple[str, str]] = set()  # 已入队（待跑或在跑）的 pair
        # pair → (下次可探测时刻, 下次失败后的退避秒数)；成功/失败都写这张表
        self._schedule: dict[tuple[str, str], tuple[float, float]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sink: Callable[[str, str, dict], None] | None = None

    # ------------------------------------------------------------------ 接线
    def set_sink(self, sink: Callable[[str, str, dict], None]) -> None:
        """设置探测结果接收方（流量统计的 note_probe_result），后置注入破除循环依赖"""
        self._sink = sink

    # ------------------------------------------------------------------ 入队（热路径）
    def enqueue(self, bind: str, host: str) -> None:
        """把一个 (代理, 域名) 交给队列（同步、O(1)，可在 modstat 热路径调用）

        跳过目标 / 在途 / 冷却期内直接返回，不占用队列；实际探测由 run() 的常驻
        消费者立刻发起（入队即干活）。
        """
        key = (bind, host)
        if _skip_target(host) or key in self._inflight:
            return
        if time.time() < self._schedule.get(key, (0.0, 0.0))[0]:
            return
        self._inflight.add(key)
        self._queue.put_nowait((bind, host))

    def is_pending(self, bind: str, host: str) -> bool:
        """该 pair 是否已入队但尚未跑出终态（供流量统计延后落盘判断）"""
        return (bind, host) in self._inflight

    # ------------------------------------------------------------------ 消费者
    async def run(self) -> None:
        """常驻消费者：取一个探测一个，立刻起任务（受信号量限并发），不轮询"""
        try:
            while True:
                bind, host = await self._queue.get()
                # 取到信号量再起任务，队列积压时形成背压（不在空闲时占用名额）
                await self._sem.acquire()
                task = asyncio.create_task(self._probe(bind, host))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except asyncio.CancelledError:
            # 退出时收掉在跑任务（各自 finally 释放信号量并清在途标记）
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            raise

    async def _probe(self, bind: str, host: str) -> None:
        """单个 pair 的探测任务（信号量已持有）：跑完释放名额并清在途标记"""
        try:
            await self._probe_once(bind, host)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 探测链路意外异常不该打死消费者：记日志，视为一次失败进入退避
            self._note_failure((bind, host), time.time())
            # 持久化级日志不带目标域名，只记代理地址
            logger.exception("DNS 探测任务异常（bind=%s）", bind)
        finally:
            self._inflight.discard((bind, host))
            self._sem.release()

    async def _probe_once(self, bind: str, host: str) -> None:
        """执行一次探测：代理反查 → DoH（IP 字面量跳过）→ 定位 → 交回结果"""
        key = (bind, host)
        proxy = self._proxy_lookup(bind)
        if proxy is None:
            # 代理已离队：不探测、不落盘、不退避（回归队列后有新会话会再入队）
            logger.debug("DNS 探测跳过（代理已离队）: %s (%s)", host, bind)
            return
        if _ip_literal(host) is not None:
            row = await self._locate_row([host])  # IP 字面量无解析语义，直接定位
            self._note_success(key, time.time())
            self._emit(bind, host, row)
            return
        try:
            addresses = await self._resolver.resolve(host, proxy=proxy)
        except ValueError as exc:
            # 主机名本身非法（IDN 编码失败等）：跳过，不退避、不落盘
            logger.debug("DNS 探测跳过非法主机名: %s (%s) err=%s", host, bind, exc)
            return
        except Exception as exc:
            self._note_failure(key, time.time())
            logger.debug("DNS 探测失败: %s (%s) err=%s", host, bind, exc)
            return
        if not addresses:
            # 解析成功但无 A 记录：负结果照常交回（消费方落空行，TTL 内不重探）
            self._note_success(key, time.time())
            self._emit(bind, host, {})
            return
        row = await self._locate_row(addresses[:_MAX_GEO_IPS])
        self._note_success(key, time.time())
        self._emit(bind, host, row)

    async def _locate_row(self, addresses: list[str]) -> dict:
        """对地址逐个定位（最多 _MAX_GEO_IPS 个），取首个定位成功的写行（调用方保证非空）

        全部未命中（保留段等）或未配置 geo 时落 ip 非空、geo 全空的行——消费方不使用，
        但 TTL 内不重复探测（避免死磕定位不出的地址）。
        """
        if self._geo is None:
            return {"ip": addresses[0]}
        for ip in addresses:
            entry = (await self._geo.resolve([ip], label="dns_probe")).get(ip)
            if entry:
                return {
                    "ip": ip,
                    "country": entry.get("country"),
                    "country_code": entry.get("country_code"),
                    "province": entry.get("province"),
                    "city": entry.get("city"),
                    "lat": entry.get("lat"),
                    "lng": entry.get("lng"),
                }
        return {"ip": addresses[0]}

    def _emit(self, bind: str, host: str, row: dict) -> None:
        """把探测结果交回接收方（流量统计落盘）；未接线时记日志丢弃，不外抛

        WARNING/异常级日志按隐私口径不带目标域名（只记代理地址）。
        """
        if self._sink is None:
            logger.warning("DNS 探测结果无接收方，丢弃（bind=%s）", bind)
            return
        try:
            self._sink(bind, host, row)
        except Exception:
            logger.exception("DNS 探测结果交付失败（bind=%s）", bind)

    # ------------------------------------------------------------------ 调度记忆
    def _note_success(self, key: tuple[str, str], now: float) -> None:
        """成功/负结果：冷却 cache_ttl，并复位退避档位"""
        self._schedule[key] = (now + self._cache_ttl, _BACKOFF_INITIAL)

    def _note_failure(self, key: tuple[str, str], now: float) -> None:
        """失败退避（60s 起指数翻倍，封顶 15min）"""
        _, delay = self._schedule.get(key, (0.0, _BACKOFF_INITIAL))
        self._schedule[key] = (now + delay, min(delay * 2, _BACKOFF_MAX))

    def prune(self, now: float) -> None:
        """清理长时间未再被触发的调度记忆（最后调度时刻早于 now - cache_ttl 即丢弃）"""
        stale = [key for key, (next_ts, _) in self._schedule.items()
                 if next_ts < now - self._cache_ttl]
        for key in stale:
            del self._schedule[key]


def _ip_literal(host: str):
    """host 是 IP 字面量则返回 ipaddress 对象，否则 None"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _skip_target(host: str) -> bool:
    """跳过内网/保留地址、单标签主机名、.local 后缀（不入队）

    这些几乎必然是内网名或本机地址，穿代理探测没有意义；公网 IP 字面量保留
    （直接定位，不需要 DoH）。
    """
    ip = _ip_literal(host)
    if ip is not None:
        return (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified)
    return "." not in host or host.endswith(".local")