"""目标服务器地区探测用例：把 (代理, 目标) 活跃流量变成「该代理出口视角的落点」

数据源是 PproxyServer 的 per-pair 流量维度（active_pairs）；探测链路 = 穿该代理做
DoH 解析（HostResolver，DNS 查询从代理出口发出）→ GeoLite2 定位 → 写 target_geo_cache。
只有面板最近请求过 /api/globe（touch）才探测——没人看地球时不消耗代理。

节奏（serve 的探测循环每 2s 调用一次 probe_cycle）：
- 候选：probe_window 秒内活跃的 pair ∩ 当前服务队列代理（代理已离队即跳过）；
- 缓存：cache_ttl 内的行（含"已探测无结果"的负结果行）不重探；
- 失败退避：pair 级内存指数退避（60s 起，封顶 15min），成功清零；
- 并发：Semaphore 限制 probe_concurrency，每轮最多 probe_concurrency * 2 个（最近活跃优先）；
- 跳过：内网/保留地址、单标签主机名、.local 后缀不探测不落盘；IP 字面量跳过 DoH
  直接定位（无解析语义）；
- 观测：/api/globe 附带 stats()（累计 DoH 成功/失败、退避中 pair 数、最近一轮候选/
  发起/待探测计数）——只有计数，不带 host，评估探测成功率不额外暴露浏览目标。

并发模型：probe_cycle 全部在事件循环单线程内执行；store 的批量读/写在 gather 前后
同步完成（SQLite 连接线程绑定），并发任务只做网络与内存操作。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Callable, Protocol

from src.domain.ports import HostResolver, TargetGeoStore

logger = logging.getLogger(__name__)

# 失败退避：60s 起指数翻倍，封顶 15min（成功清零）
_BACKOFF_INITIAL = 60.0
_BACKOFF_MAX = 900.0
# 缓存清理节流：探测循环每 2s 一轮，实际落库清理每小时一次
_PRUNE_INTERVAL = 3600.0
# 每个解析结果最多定位的 A 记录数（多地址只增定位开销，取首个命中即可）
_MAX_GEO_IPS = 2
# 每个「代理→目标城市」分组最多带出的 host 数（tooltip 用）
_MAX_HOSTS_PER_GROUP = 8


class PairTrafficSource(Protocol):
    """活跃 (bind, host) 流量源（PproxyServer 实现）"""

    def active_pairs(self, since_ts: float) -> list[dict]: ...


class GeoResolver(Protocol):
    """IP 归属查询（IpGeoUseCase 实现）：缓存优先，不外抛"""

    async def resolve(self, ips: list[str], *, label: str) -> dict[str, dict]: ...


class TargetGeoUseCase:
    """目标地区探测用例（激活窗口 + 缓存 TTL + 失败退避 + 限并发）"""

    def __init__(
        self,
        *,
        server: PairTrafficSource,
        store: TargetGeoStore,
        resolver: HostResolver,
        geo: GeoResolver,
        proxy_lookup: Callable[[str], str | None],
        probe_concurrency: int,
        cache_ttl: float,
        activation_window: float,
        probe_window: float,
        prune_after: float,
    ) -> None:
        """
        Args:
            server:           活跃 pair 流量源（PproxyServer.active_pairs）
            store:            目标归属缓存（SqliteProxyStore）
            resolver:         穿代理的域名解析器（DohResolver）
            geo:              IP 归属查询（IpGeoUseCase，缓存优先）
            proxy_lookup:     bind → 代理 URL；None 表示该代理已不在服务队列（跳过探测）
            probe_concurrency: 并发探测上限
            cache_ttl:        (代理,目标) 缓存有效期秒（负结果行同样受此保护）
            activation_window: 最近多少秒内有 /api/globe 请求才探测
            probe_window:     窗口内有流量的 pair 才作为探测候选
            prune_after:      缓存行清理阈值秒（每小时按此清理一次）
        """
        self._server = server
        self._store = store
        self._resolver = resolver
        self._geo = geo
        self._proxy_lookup = proxy_lookup
        self._probe_concurrency = probe_concurrency
        self._cache_ttl = cache_ttl
        self._activation_window = activation_window
        self._probe_window = probe_window
        self._prune_after = prune_after
        self._last_touch = 0.0
        # pair → (下次可尝试时刻, 下次失败后的退避秒数)；成功后删除
        self._backoff: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_prune = 0.0
        # 观测计数：DoH 探测结果累计（IP 字面量直接定位不计）+ 最近一轮候选快照
        self._probe_ok = 0
        self._probe_fail = 0
        self._last_cycle = {"candidates": 0, "probed": 0, "pending": 0, "cycle_ts": 0.0}

    # ------------------------------------------------------------------ 激活
    def touch(self) -> None:
        """面板活跃信号（/api/globe 每次请求调用）"""
        self._last_touch = time.time()

    def is_active(self) -> bool:
        """最近 activation_window 秒内有过 /api/globe 请求"""
        return time.time() - self._last_touch <= self._activation_window

    # ------------------------------------------------------------------ 探测
    async def probe_cycle(self) -> int:
        """跑一轮探测，返回本轮发起的探测数（未激活/无候选返回 0）

        异常语义：缓存读失败跳过本轮（无法判断新鲜度，重探会放大请求）；缓存写失败
        只丢本次持久化、不中断循环（同 IpGeoUseCase 的三步降级口径）。
        """
        now = time.time()
        self._last_cycle["cycle_ts"] = now
        if not self.is_active():
            # 未激活时清掉本轮快照，避免面板显示上一轮遗留的待探测数
            self._last_cycle.update(candidates=0, probed=0, pending=0)
            return 0
        self._housekeeping(now)
        candidates = self._pick_candidates(now)
        self._last_cycle.update(candidates=len(candidates), probed=0, pending=0)
        if not candidates:
            return 0
        keys = [(pair["bind"], pair["host"]) for pair in candidates]
        try:
            cached = self._store.get_target_geo_batch(keys)
        except Exception:
            logger.exception("目标归属缓存读取失败，跳过本轮探测")
            return 0
        due = [
            pair for pair in candidates
            if not _row_fresh(cached.get((pair["bind"], pair["host"])), now, self._cache_ttl)
            and self._backoff.get((pair["bind"], pair["host"]), (0.0, 0.0))[0] <= now
        ]
        if not due:
            return 0
        # 最近活跃优先；每轮只发 limit 个，避免一次请求洪峰后的突发新 pair 撞满代理
        due.sort(key=lambda pair: pair["last_seen"], reverse=True)
        batch = due[:self._probe_concurrency * 2]
        self._last_cycle.update(probed=len(batch), pending=len(due) - len(batch))
        semaphore = asyncio.Semaphore(self._probe_concurrency)
        results = await asyncio.gather(*(self._probe_pair(pair, semaphore, now) for pair in batch))
        rows = {key: row for key, row in results if row is not None}
        if rows:
            try:
                self._store.save_target_geo_batch(rows)
            except Exception:
                logger.exception("目标归属缓存写入失败: %d 行", len(rows))
        return len(batch)

    def _pick_candidates(self, now: float) -> list[dict]:
        """候选：窗口内活跃 pair ∩ 队列代理 ∩ 非跳过目标（跳过项不占本轮探测名额）

        代理地址随候选一并快照（proxy 键）：探测任务与候选判定使用同一队列视图，
        避免两次查询之间代理离队导致用 None 发起请求。
        """
        candidates: list[dict] = []
        for pair in self._server.active_pairs(now - self._probe_window):
            bind, host = pair["bind"], pair["host"]
            proxy = self._proxy_lookup(bind)
            if proxy is None:
                continue
            if _skip_target(host):
                logger.debug("跳过目标（内网/保留地址或单标签主机名）: %s (%s)", host, bind)
                continue
            candidates.append({**pair, "proxy": proxy})
        return candidates

    async def _probe_pair(self, pair: dict, semaphore: asyncio.Semaphore,
                          now: float) -> tuple[tuple[str, str], dict | None]:
        """探测单个 pair（信号量内），返回 (key, 待落盘行 | None)

        None = 跳过（不落盘也不退避）或失败（已记退避）；空 dict = 有效但无结果。
        """
        bind, host = pair["bind"], pair["host"]
        key = (bind, host)
        proxy = pair["proxy"]  # 候选快照，不再重查队列
        async with semaphore:
            if _ip_literal(host) is not None:
                addresses = [host]  # IP 字面量无解析语义，直接定位
            else:
                try:
                    addresses = await self._resolver.resolve(host, proxy=proxy)
                except ValueError as exc:
                    # 主机名本身非法（IDN 编码失败等）：跳过，不落盘也不退避
                    logger.debug("跳过非法主机名: %s (%s) err=%s", host, bind, exc)
                    return key, None
                except Exception as exc:
                    self._probe_fail += 1
                    self._note_failure(key, now)
                    logger.debug("目标 DNS 探测失败: %s (%s) err=%s", host, bind, exc)
                    return key, None
                # DoH 查询本身成功（含无 A 记录的负结果）；IP 字面量不走这里，不计入
                self._probe_ok += 1
                if not addresses:
                    # 解析成功但无 A 记录：负结果照常落盘，TTL 内不重探
                    self._note_success(key)
                    return key, {}
            row = await self._locate_row(addresses[:_MAX_GEO_IPS])
            self._note_success(key)
            logger.debug("目标探测完成: %s (%s) ip=%s geo=%s",
                         host, bind, row.get("ip"), row.get("city") or row.get("country"))
            return key, row

    async def _locate_row(self, addresses: list[str]) -> dict:
        """对地址逐个定位（最多 _MAX_GEO_IPS 个），取首个定位成功的写行（调用方保证非空）

        全部未命中（保留段等）时落 ip 非空、geo 全空的行——地球不使用，但 TTL 内
        不重复探测（避免死磕定位不出的地址）。
        """
        for ip in addresses:
            entry = (await self._geo.resolve([ip], label="target")).get(ip)
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

    # ------------------------------------------------------------------ 观测
    def stats(self, now: float | None = None) -> dict:
        """探测观测快照（/api/globe 下发；只有计数，不含 host）

        probe_ok/probe_fail 为进程内累计的 DoH 探测结果（IP 字面量直接定位不计）；
        backoff 为此刻仍在退避等待中的 pair 数；candidates/probed/pending 为最近一轮
        候选、本轮发起与"想做但未排上"的数量；cycle_ts 为最近一轮探测循环时刻。
        """
        now = time.time() if now is None else now
        return {
            "active": self.is_active(),
            "probe_ok": self._probe_ok,
            "probe_fail": self._probe_fail,
            "backoff": sum(1 for next_ts, _ in self._backoff.values() if next_ts > now),
            **self._last_cycle,
        }

    # ------------------------------------------------------------------ 退避与清理
    def _note_failure(self, key: tuple[str, str], now: float) -> None:
        """记失败退避（60s 起指数翻倍，封顶 15min）"""
        _, delay = self._backoff.get(key, (0.0, _BACKOFF_INITIAL))
        self._backoff[key] = (now + delay, min(delay * 2, _BACKOFF_MAX))

    def _note_success(self, key: tuple[str, str]) -> None:
        self._backoff.pop(key, None)

    def _housekeeping(self, now: float) -> None:
        """内存退避记录清理 + 每小时一次过期缓存行清理

        退避记录在最后一次失败后 cache_ttl 内没有再被尝试（pair 已离开候选/不再活跃）
        即丢弃，避免长期运行下随历史 pair 无界增长。
        """
        stale = [key for key, (next_ts, _) in self._backoff.items()
                 if next_ts < now - self._cache_ttl]
        for key in stale:
            del self._backoff[key]
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            deleted = self._store.prune_target_geo(now - self._prune_after)
        except Exception:
            logger.exception("目标归属缓存清理失败")
            return
        if deleted:
            logger.info("清理目标归属缓存 %d 行（阈值 %.0f 天）",
                        deleted, self._prune_after / 86400)

    # ------------------------------------------------------------------ 聚合（供 /api/globe）
    @staticmethod
    def aggregate_flows(pairs: list[dict], target_rows: dict,
                        max_flows: int) -> list[dict]:
        """把活跃 pair 与目标归属聚合成「代理→目标城市」弧数据（纯函数，可单测）

        pairs: active_pairs() 输出；target_rows: get_target_geo_batch() 输出。
        按 (bind, country_code, city) 分组求和速率，hosts 每组按速率降序取前
        _MAX_HOSTS_PER_GROUP 个；无坐标的行不产出弧；按总速率降序截断到 max_flows。
        返回 [{bind, country, country_code, city, lat, lng, up_rate, down_rate,
               up_inst, down_inst, hosts}]（速率单位 B/s）。
        """
        groups: dict[tuple, dict] = {}
        for pair in pairs:
            row = target_rows.get((pair["bind"], pair["host"]))
            if row is None or row.get("lat") is None or row.get("lng") is None:
                continue
            key = (pair["bind"], row.get("country_code") or "", row.get("city") or "")
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = {
                    "bind": pair["bind"],
                    "country": row.get("country"),
                    "country_code": row.get("country_code"),
                    "city": row.get("city"),
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "up_rate": 0.0,
                    "down_rate": 0.0,
                    "up_inst": 0.0,
                    "down_inst": 0.0,
                    "_host_rates": [],
                }
            entry["up_rate"] += pair.get("up_rate", 0.0)
            entry["down_rate"] += pair.get("down_rate", 0.0)
            entry["up_inst"] += pair.get("up_inst", 0.0)
            entry["down_inst"] += pair.get("down_inst", 0.0)
            entry["_host_rates"].append(
                (pair.get("up_rate", 0.0) + pair.get("down_rate", 0.0), pair["host"])
            )
        flows: list[dict] = []
        for entry in groups.values():
            host_rates = sorted(entry.pop("_host_rates"), reverse=True)
            entry["hosts"] = [host for _, host in host_rates[:_MAX_HOSTS_PER_GROUP]]
            flows.append(entry)
        flows.sort(key=lambda flow: flow["up_rate"] + flow["down_rate"], reverse=True)
        return flows[:max_flows]

    def build_flows(self, pairs: list[dict], max_flows: int) -> list[dict]:
        """把活跃 pair 聚合成「代理→目标」弧数据（/api/globe 消费）

        批量读目标归属缓存后交给 aggregate_flows；读失败记日志并返回空——单次
        DB 抖动只丢本帧目标弧，不把整个地球请求打成 500（与探测侧读失败降级同口径）。
        """
        if not pairs:
            return []
        try:
            rows = self._store.get_target_geo_batch([(pair["bind"], pair["host"]) for pair in pairs])
        except Exception:
            logger.exception("目标归属缓存读取失败，本次不下发目标弧")
            return []
        return self.aggregate_flows(pairs, rows, max_flows)


def _ip_literal(host: str):
    """host 是 IP 字面量则返回 ipaddress 对象，否则 None"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _skip_target(host: str) -> bool:
    """跳过内网/保留地址、单标签主机名、.local 后缀（不探测不落盘）

    这些几乎必然是内网名或本机地址，穿代理探测没有意义；公网 IP 字面量保留
    （直接定位，不需要 DoH）。
    """
    ip = _ip_literal(host)
    if ip is not None:
        return (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified)
    return "." not in host or host.endswith(".local")


def _row_fresh(row: dict | None, now: float, ttl: float) -> bool:
    """缓存行是否仍在 TTL 内（None = 无缓存）"""
    return row is not None and (now - row["cached_at"]) < ttl
