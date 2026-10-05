"""流量统计用例（use_cases 层）：全局 / 按代理 / 按 (代理,目标) 三个维度的实时计数与采样，
外加活跃连接计数（连接级）与历史流量条目的持久化（按 (代理,域名,IP) 分区）。

数据源是 pproxy 的回调，但不感知 pproxy：PproxyServer 的 modstat 钩子把上行/下行字节与
连接变化转发成 record_bytes / record_conn 调用，本模块只维护统计状态。

口径：
- 全局与按代理的累计字节是「进程启动至今」的累加（重启清零）；
- (代理, 目标) 的 pair 只保留 metrics_window 内的活跃项，超 _MAX_PAIRS 按 last_seen 批量淘汰；
- 历史条目持久化到 store，写入是**增量累加**——重启后内存增量归零也不会冲掉已落盘的历史。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, NamedTuple

from src.domain.ports import TrafficHistoryStore

logger = logging.getLogger(__name__)

# (bind, host) 维度 pair 上限：浏览类流量会不断产生新 host，不设上限就是内存泄漏；
# 超限按 last_seen 批量淘汰最旧的一批——批量而非逐个淘汰，避免高频 churn 下每次
# 新建 pair 都做一次 O(n) 扫描。
_MAX_PAIRS = 2000
_PAIR_EVICT_BATCH = 256
# pair 速率 EMA 平滑系数：线宽用 EMA（防抖动），瞬时速率只用于流速
_PAIR_RATE_EMA_ALPHA = 0.3
# 历史条目清理节流（秒）：保留期判断在采样循环里做，但不必每轮都打一次 DELETE
_PRUNE_INTERVAL = 3600.0


class TrafficSample(NamedTuple):
    """流量采样点（累计字节），与 StandbySample 同机制：NamedTuple 紧凑存储 + deque 按时间窗口清理"""
    ts: float
    total_upload: int
    total_download: int


def normalize_host(host: str) -> str:
    """目标主机名归一化：小写、去首尾空白与结尾点；空串表示不记录

    同一目标可能以 "Example.COM." / "example.com" 等不同形式出现（不同客户端/协议），
    不归一会把一个目标拆成多个 pair。
    """
    return host.strip().rstrip(".").lower()


class TrafficStatsUseCase:
    """流量统计：实时计数 + 采样序列 + 活跃连接 + 历史条目落盘

    全部状态在事件循环线程访问（pproxy 回调与采样循环同线程），无锁。
    """

    def __init__(
        self,
        *,
        store: TrafficHistoryStore,
        target_ip_lookup: Callable[[list[tuple[str, str]]], dict[tuple[str, str], str]],
        metrics_window: float,
        flush_interval: float,
        retention: float,
        max_pairs: int = _MAX_PAIRS,
    ) -> None:
        """
        Args:
            store:            历史条目存储（增量累加写入）
            target_ip_lookup: (bind, host) 批量查目标 IP（落盘时给条目定 IP 维度；
                              取不到返回空 dict，对应条目 IP 记空串）
            metrics_window:   采样数据留存窗口（秒），pair 与采样序列按此清理
            flush_interval:   历史条目落盘节流间隔（秒）
            retention:        历史条目保留期（秒），过期由 prune 删除
            max_pairs:        (bind, host) 维度容量上限
        """
        self._store = store
        self._target_ip_lookup = target_ip_lookup
        self._metrics_window = metrics_window
        self._flush_interval = flush_interval
        self._retention = retention
        self._max_pairs = max_pairs
        # 全局累计
        self._upload = 0
        self._download = 0
        self._traffic_history: deque[TrafficSample] = deque()
        # 按上游代理地址（bind）的累计与采样历史
        self._per_proxy_upload: dict[str, int] = {}
        self._per_proxy_download: dict[str, int] = {}
        self._per_proxy_traffic_history: dict[str, deque[TrafficSample]] = {}
        # (bind, host) 维度：目标地区探测与地球飞线的数据源
        self._pair_last_seen: dict[tuple[str, str], float] = {}
        self._pair_upload: dict[tuple[str, str], int] = {}
        self._pair_download: dict[tuple[str, str], int] = {}
        self._pair_rate_ema: dict[tuple[str, str], tuple[float, float]] = {}
        self._pair_rate_inst: dict[tuple[str, str], tuple[float, float]] = {}
        self._pair_prev: dict[tuple[str, str], tuple[int, int]] = {}
        self._pair_sample_ts: float | None = None
        # 活跃连接计数（连接级）：归零即删，不保留余温
        self._conn_count: dict[tuple[str, str], int] = {}
        self._conn_first_seen: dict[tuple[str, str], float] = {}
        # 历史条目待落盘增量：{(bind, host): {"up", "down", "last_seen"}}
        self._pending: dict[tuple[str, str], dict[str, float]] = {}
        self._last_flush = 0.0
        self._last_prune = 0.0

    # ------------------------------------------------------------------ 上报（modstat 热路径）
    def record_bytes(self, bind: str, host: str, up: int, down: int) -> None:
        """上报一个方向的字节增量（up/down 之一恒为 0，两个方向来自不同通道）

        host 在此归一化（调用方直接透传 pproxy 的原始 host_name）；空 host 只计入
        全局与按代理维度，不建 pair。
        """
        if up:
            self._upload += up
            self._per_proxy_upload[bind] = self._per_proxy_upload.get(bind, 0) + up
        if down:
            self._download += down
            self._per_proxy_download[bind] = self._per_proxy_download.get(bind, 0) + down
        host = normalize_host(host)
        if not host:
            return
        key = (bind, host)
        if up:
            self._pair_upload[key] = self._pair_upload.get(key, 0) + up
        if down:
            self._pair_download[key] = self._pair_download.get(key, 0) + down

    def record_conn(self, bind: str, host: str, delta: int) -> None:
        """上报连接数变化（+1 建立 / -1 关闭），按 (bind, host) 计数

        归零即删（不保留余温）；异常路径下 -1 多于 +1 时钳到 0 并记日志，不出现负计数。
        """
        host = normalize_host(host)
        if not host:
            return
        key = (bind, host)
        current = self._conn_count.get(key, 0) + delta
        if current > 0:
            # 记录首个连接建立时刻（同 key 的并发连接只记第一次）
            self._conn_first_seen.setdefault(key, time.time())
            self._conn_count[key] = current
        elif current < 0:
            # 只记代理地址，不带目标域名（日志按 WARNING 落盘，域名不该进常规日志）
            logger.warning("连接计数出现负数，已钳零: bind=%s", bind)
            self._conn_count.pop(key, None)
            self._conn_first_seen.pop(key, None)
        else:
            self._conn_count.pop(key, None)
            self._conn_first_seen.pop(key, None)

    def note_pair(self, bind: str, host: str) -> None:
        """记录 (bind, host) 的会话活跃时间（会话建立时调用）

        last_seen 是「最近一次会话建立」而非字节时间：探测候选只关心最近被请求过的
        目标，长时间只读的长连接会随窗口滑出候选（其 host 通常还有其它新会话）。
        """
        host = normalize_host(host)
        if not host:
            return
        key = (bind, host)
        self._pair_last_seen[key] = time.time()
        self._pair_prev.setdefault(key, (0, 0))
        if len(self._pair_last_seen) > self._max_pairs:
            self._evict_pairs()

    # ------------------------------------------------------------------ 采样
    def sample(self, now: float) -> None:
        """采当前累计流量到历史序列、算 pair 速率/EMA、并入历史待落盘增量

        与 StandbyQueueUseCase._sample 同机制：差分出瞬时速率，按时间窗口清理过期点。
        落盘按 flush_interval 节流（pair 可达数千，不能每轮写库）。
        """
        dt = now - self._pair_sample_ts if self._pair_sample_ts is not None else 0.0
        for key in self._pair_last_seen:
            up = self._pair_upload.get(key, 0)
            down = self._pair_download.get(key, 0)
            prev_up, prev_down = self._pair_prev[key]
            # 增量与 dt 无关：首采（prev 为 0）也要把建连以来的字节记入历史
            delta_up = max(0, up - prev_up)
            delta_down = max(0, down - prev_down)
            if dt > 0:
                rate_up = delta_up / dt
                rate_down = delta_down / dt
            else:
                # 首采 dt=0：不把建连以来的累计冲量当瞬时速率
                rate_up = rate_down = 0.0
            ema_up, ema_down = self._pair_rate_ema.get(key, (0.0, 0.0))
            self._pair_rate_ema[key] = (
                ema_up + _PAIR_RATE_EMA_ALPHA * (rate_up - ema_up),
                ema_down + _PAIR_RATE_EMA_ALPHA * (rate_down - ema_down),
            )
            self._pair_rate_inst[key] = (rate_up, rate_down)
            self._pair_prev[key] = (up, down)
            if delta_up or delta_down:
                self._add_pending(key, delta_up, delta_down, self._pair_last_seen[key])
        self._pair_sample_ts = now

        cutoff = now - self._metrics_window
        stale = [key for key, ts in self._pair_last_seen.items() if ts < cutoff]
        for key in stale:
            self._drop_pair(key)
        if stale:
            logger.debug("清理过期 pair %d 个（窗口 %.0fs）", len(stale), self._metrics_window)

        self._traffic_history.append(TrafficSample(now, self._upload, self._download))
        while self._traffic_history and self._traffic_history[0].ts < cutoff:
            self._traffic_history.popleft()

        for bind in set(self._per_proxy_upload) | set(self._per_proxy_download):
            dq = self._per_proxy_traffic_history.get(bind)
            if dq is None:
                dq = deque()
                self._per_proxy_traffic_history[bind] = dq
            dq.append(TrafficSample(now, self._per_proxy_upload.get(bind, 0),
                                    self._per_proxy_download.get(bind, 0)))
            while dq and dq[0].ts < cutoff:
                dq.popleft()

        self.flush(now)

    def flush(self, now: float, *, force: bool = False) -> None:
        """把待落盘增量写入历史条目（按 flush_interval 节流；force=True 时不受节流约束）

        条目 IP 维度在此确定：批量查 (bind, host) 当前解析出的目标 IP，取不到记空串。
        IP 变了就是新条目——旧条目自然冻结在切换前的累计值，不再更新。
        写失败保留待落盘增量（下次重试），只记 exception 不中断采样。
        """
        if not self._pending:
            return
        if not force and now - self._last_flush < self._flush_interval:
            return
        self._last_flush = now
        try:
            pairs = list(self._pending)
            rows = self._target_ip_lookup(pairs)
            deltas = {
                (bind, host, (rows.get((bind, host)) or "")): d
                for (bind, host), d in self._pending.items()
            }
            self._store.add_traffic_history(deltas)
        except Exception:
            logger.exception("历史流量落盘失败，本轮增量保留待重试（%d 条）", len(self._pending))
            return
        self._pending.clear()
        self._prune(now)

    # ------------------------------------------------------------------ 查询
    def total(self) -> tuple[int, int]:
        """当前累计上行/下行字节"""
        return self._upload, self._download

    def traffic_history(self) -> list[TrafficSample]:
        """全局流量采样历史（按时间正序），供仪表盘图表"""
        return list(self._traffic_history)

    def per_proxy_traffic(self) -> dict[str, dict[str, int]]:
        """按上游代理地址（bind）的累计上下行字节"""
        binds = set(self._per_proxy_upload) | set(self._per_proxy_download)
        return {bind: {
            "upload": self._per_proxy_upload.get(bind, 0),
            "download": self._per_proxy_download.get(bind, 0),
        } for bind in binds}

    def per_proxy_traffic_history(self, bind: str) -> list[TrafficSample]:
        """指定上游代理地址的流量采样历史（按时间正序），供单代理趋势查询"""
        return list(self._per_proxy_traffic_history.get(bind, ()))

    def active_pairs(self, since_ts: float) -> list[dict]:
        """窗口内活跃的 (bind, host) 列表（last_seen ≥ since_ts），供目标探测与地球飞线

        返回 [{bind, host, up, down, up_rate, down_rate, up_inst, down_inst, last_seen}]：
        up_rate/down_rate 为 EMA（防抖，线宽用），up_inst/down_inst 为瞬时速率（B/s，流速用）。
        """
        out: list[dict] = []
        for key, last_seen in self._pair_last_seen.items():
            if last_seen < since_ts:
                continue
            ema_up, ema_down = self._pair_rate_ema.get(key, (0.0, 0.0))
            inst_up, inst_down = self._pair_rate_inst.get(key, (0.0, 0.0))
            out.append({
                "bind": key[0],
                "host": key[1],
                "up": self._pair_upload.get(key, 0),
                "down": self._pair_download.get(key, 0),
                "up_rate": ema_up,
                "down_rate": ema_down,
                "up_inst": inst_up,
                "down_inst": inst_down,
                "last_seen": last_seen,
            })
        return out

    def live_connections(self) -> list[dict]:
        """此刻仍开着的连接（连接级口径）：按 (bind, host) 汇总，连接数降序

        返回 [{bind, host, ip, connections, up_rate, down_rate, first_seen, last_seen}]。
        目标 IP 由探测缓存给出（尚未解析为 None）；活跃连接数很少，这里的批量查询可忽略。
        """
        pairs = list(self._conn_count)
        ips = self._target_ip_lookup(pairs) if pairs else {}
        out = []
        for key, count in self._conn_count.items():
            ema_up, ema_down = self._pair_rate_ema.get(key, (0.0, 0.0))
            out.append({
                "bind": key[0],
                "host": key[1],
                "ip": ips.get(key) or None,
                "connections": count,
                "up_rate": ema_up,
                "down_rate": ema_down,
                "first_seen": self._conn_first_seen.get(key, 0.0),
                "last_seen": self._pair_last_seen.get(key, 0.0),
            })
        out.sort(key=lambda x: x["connections"], reverse=True)
        return out

    def history_entries(self, limit: int) -> list[dict]:
        """历史流量条目（按流量总计降序，最多 limit 条）"""
        return self._store.get_traffic_history(limit)

    def history_count(self) -> int:
        """历史条目总数（不受 limit 截断影响）"""
        return self._store.count_traffic_history()

    # ------------------------------------------------------------------ 内部
    def _add_pending(self, key: tuple[str, str], up: int, down: int, last_seen: float) -> None:
        """把一个采样窗口的 pair 增量并入待落盘（同 key 累加；last_seen 取最新）"""
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {"up": up, "down": down, "last_seen": last_seen}
        else:
            entry["up"] += up
            entry["down"] += down
            entry["last_seen"] = max(entry["last_seen"], last_seen)

    def _prune(self, now: float) -> None:
        """按保留期清理历史条目（节流为一小时一次）"""
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            removed = self._store.prune_traffic_history(now - self._retention)
        except Exception:
            logger.exception("历史流量清理失败")
            return
        if removed:
            logger.info("清理历史流量条目 %d 条（保留期 %.0fs）", removed, self._retention)

    def _evict_pairs(self) -> None:
        """超容量时按 last_seen 最旧批量淘汰（六个 pair dict 同步删，不动全局/按代理累计）"""
        over = len(self._pair_last_seen) - self._max_pairs
        count = min(len(self._pair_last_seen), over + _PAIR_EVICT_BATCH)
        victims = sorted(self._pair_last_seen, key=self._pair_last_seen.get)[:count]
        for key in victims:
            self._drop_pair(key)
        logger.debug("pair 超容量，淘汰最旧 %d 个（当前 %d）", count, len(self._pair_last_seen))

    def _drop_pair(self, key: tuple[str, str]) -> None:
        """删除一个 pair 的全部内存记录（待落盘增量不动——流量确实发生过，照常落盘）"""
        self._pair_last_seen.pop(key, None)
        self._pair_upload.pop(key, None)
        self._pair_download.pop(key, None)
        self._pair_rate_ema.pop(key, None)
        self._pair_rate_inst.pop(key, None)
        self._pair_prev.pop(key, None)
