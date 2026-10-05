"""流量统计用例（TrafficStatsUseCase）测试

覆盖面：全局/按代理/pair 三个维度的计数与采样、连接级计数、历史条目落盘（增量累加）。
"""
import time
import unittest
from unittest.mock import patch

from src.use_cases.traffic_stats import TrafficStatsUseCase

# 用例内部读时钟的模块路径（note_pair / record_conn 都取 time.time）
_TIME_PATH = "src.use_cases.traffic_stats.time.time"


class FakeStore:
    """内存版历史条目存储（增量累加语义与 SqliteProxyStore 一致）"""

    def __init__(self):
        self.rows: dict[tuple[str, str, str], dict] = {}

    def add_traffic_history(self, deltas):
        for (bind, host, ip), d in deltas.items():
            row = self.rows.setdefault((bind, host, ip), {
                "bind": bind, "host": host, "ip": ip,
                "upload": 0, "download": 0, "last_seen": 0.0,
            })
            row["upload"] += d["up"]
            row["download"] += d["down"]
            row["last_seen"] = max(row["last_seen"], d["last_seen"])

    def get_traffic_history(self, limit):
        rows = sorted(self.rows.values(), key=lambda r: -(r["upload"] + r["download"]))
        return [dict(r) for r in rows[:limit]]

    def count_traffic_history(self):
        return len(self.rows)

    def prune_traffic_history(self, before_ts):
        victims = [key for key, row in self.rows.items() if row["last_seen"] < before_ts]
        for key in victims:
            del self.rows[key]
        return len(victims)


def _make(store=None, ip_map=None, **kwargs):
    """构造用例：默认 fake store + 按 ip_map 回答目标 IP 查询"""
    store = store if store is not None else FakeStore()
    ip_map = ip_map or {}
    stats = TrafficStatsUseCase(
        store=store,
        target_ip_lookup=lambda pairs: {p: ip_map[p] for p in pairs if p in ip_map},
        metrics_window=kwargs.pop("metrics_window", 1800.0),
        flush_interval=kwargs.pop("flush_interval", 15.0),
        retention=kwargs.pop("retention", 2592000.0),
        **kwargs,
    )
    return stats, store


class TestGlobalAndPerProxy(unittest.TestCase):
    """全局与按代理两个维度的累计"""

    def test_empty_initially(self):
        stats, _ = _make()
        self.assertEqual(stats.per_proxy_traffic(), {})
        self.assertEqual(stats.total(), (0, 0))

    def test_record_bytes_per_bind_and_total(self):
        """按 bind 累计，全局总计与 per-proxy 之和一致"""
        stats, _ = _make()
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.record_bytes("1.1.1.1:8080", "example.com", 0, 200)
        stats.record_bytes("2.2.2.2:1080", "example.com", 50, 0)
        stats.record_bytes("2.2.2.2:1080", "example.com", 0, 150)
        traffic = stats.per_proxy_traffic()
        self.assertEqual(traffic["1.1.1.1:8080"], {"upload": 100, "download": 200})
        self.assertEqual(traffic["2.2.2.2:1080"], {"upload": 50, "download": 150})
        self.assertEqual(stats.total(), (150, 350))

    def test_direct_bind(self):
        """显式 direct:// 上游（bind == "DIRECT"）仍用 'DIRECT' 作 key"""
        stats, _ = _make()
        stats.record_bytes("DIRECT", "example.com", 500, 0)
        stats.record_bytes("DIRECT", "example.com", 0, 800)
        self.assertEqual(stats.per_proxy_traffic()["DIRECT"], {"upload": 500, "download": 800})

    def test_empty_host_counts_global_only(self):
        """空 host 不建 pair，但全局与按代理计数不受影响"""
        stats, _ = _make()
        stats.record_bytes("1.1.1.1:8080", "", 100, 0)
        self.assertEqual(stats.active_pairs(0), [])
        self.assertEqual(stats.per_proxy_traffic()["1.1.1.1:8080"],
                         {"upload": 100, "download": 0})


class TestPairSampling(unittest.TestCase):
    """(bind, host) 维度的归一化、速率差分与清理"""

    def test_note_pair_normalizes_host(self):
        """host 归一化（大小写/尾点/空白），字节与 last_seen 入 pair"""
        stats, _ = _make()
        with patch(_TIME_PATH, return_value=1000.0):
            stats.note_pair("1.1.1.1:8080", " Example.COM. ")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 300)
        pairs = stats.active_pairs(0)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["bind"], "1.1.1.1:8080")
        self.assertEqual(pairs[0]["host"], "example.com")
        self.assertEqual((pairs[0]["up"], pairs[0]["down"]), (100, 300))
        self.assertEqual((pairs[0]["up_rate"], pairs[0]["down_rate"]), (0.0, 0.0))
        self.assertEqual(pairs[0]["last_seen"], 1000.0)

    def test_active_pairs_window_filter(self):
        """since_ts 之前的 last_seen 被过滤（界内包含）"""
        stats, _ = _make()
        with patch(_TIME_PATH, return_value=1000.0):
            stats.note_pair("1.1.1.1:8080", "example.com")
        self.assertEqual(len(stats.active_pairs(1000.0)), 1)
        self.assertEqual(stats.active_pairs(1000.5), [])

    def test_sample_computes_instant_rate_ema(self):
        """首采速率 0（防累计冲量误判），次采按差分算瞬时，EMA=0.3*瞬时"""
        stats, _ = _make()
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        stats.record_bytes("1.1.1.1:8080", "example.com", 200, 0)
        stats.record_bytes("1.1.1.1:8080", "example.com", 0, 210)
        stats.sample(1002.0)
        pair = stats.active_pairs(0)[0]
        self.assertEqual((pair["up"], pair["down"]), (300, 210))
        self.assertAlmostEqual(pair["up_rate"], 0.3 * 200 / 2)
        self.assertAlmostEqual(pair["down_rate"], 0.3 * 210 / 2)
        self.assertAlmostEqual(pair["up_inst"], 200 / 2)
        self.assertAlmostEqual(pair["down_inst"], 210 / 2)

    def test_stale_pairs_cleaned_with_window(self):
        """last_seen 超出 metrics_window 的 pair 连六个 dict 一起清，不留孤儿"""
        stats, _ = _make(metrics_window=10.0)
        with patch(_TIME_PATH, return_value=1000.0):
            stats.note_pair("1.1.1.1:8080", "example.com")
        stats.sample(1005.0)
        self.assertEqual(len(stats.active_pairs(0)), 1)
        stats.sample(1011.0)
        self.assertEqual(stats.active_pairs(0), [])
        self.assertEqual(stats._pair_upload, {})
        self.assertEqual(stats._pair_download, {})
        self.assertEqual(stats._pair_rate_ema, {})
        self.assertEqual(stats._pair_rate_inst, {})
        self.assertEqual(stats._pair_prev, {})

    def test_capacity_evicts_oldest_pairs(self):
        """超 max_pairs 时按 last_seen 批量淘汰最旧"""
        stats, _ = _make(max_pairs=3)
        with patch("src.use_cases.traffic_stats._PAIR_EVICT_BATCH", 1):
            for i, ts in enumerate((100.0, 200.0, 300.0, 400.0)):
                with patch(_TIME_PATH, return_value=ts):
                    stats.note_pair("1.1.1.1:8080", f"h{i}.example")
        hosts = {p["host"] for p in stats.active_pairs(0)}
        self.assertEqual(hosts, {"h2.example", "h3.example"})


class TestPerProxyHistory(unittest.TestCase):
    """按代理的流量采样序列"""

    def test_empty_initially(self):
        stats, _ = _make()
        self.assertEqual(stats.per_proxy_traffic_history("1.1.1.1:8080"), [])

    def test_sample_records_per_bind(self):
        """sample 为每个有流量的 bind 各采一笔"""
        stats, _ = _make()
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.record_bytes("1.1.1.1:8080", "example.com", 0, 200)
        stats.record_bytes("2.2.2.2:1080", "example.com", 50, 0)
        stats.sample(1000.0)
        h1 = stats.per_proxy_traffic_history("1.1.1.1:8080")
        h2 = stats.per_proxy_traffic_history("2.2.2.2:1080")
        self.assertEqual(len(h1), 1)
        self.assertEqual((h1[0].total_upload, h1[0].total_download), (100, 200))
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0].total_upload, 50)

    def test_multiple_samples_accumulate(self):
        """多次采样各记一笔，累计值递增"""
        stats, _ = _make()
        stats.record_bytes("1.1.1.1:8080", "example.com", 0, 100)
        stats.sample(1000.0)
        stats.record_bytes("1.1.1.1:8080", "example.com", 0, 200)
        stats.sample(1002.0)
        h = stats.per_proxy_traffic_history("1.1.1.1:8080")
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0].total_download, 100)
        self.assertEqual(h[1].total_download, 300)

    def test_window_cleanup(self):
        """超出 metrics_window 的采样点自动清理"""
        stats, _ = _make(metrics_window=0.1)
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(time.time())
        time.sleep(0.15)
        stats.record_bytes("1.1.1.1:8080", "example.com", 200, 0)
        stats.sample(time.time())
        h = stats.per_proxy_traffic_history("1.1.1.1:8080")
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0].total_upload, 300)

    def test_unknown_bind_returns_empty(self):
        stats, _ = _make()
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        self.assertEqual(stats.per_proxy_traffic_history("9.9.9.9:9999"), [])


class TestConnections(unittest.TestCase):
    """连接级统计（实时页的数据源）：连接建立 +1 / 关闭 -1，归零即删"""

    def test_count_rises_and_drops_to_empty(self):
        stats, _ = _make()
        with patch(_TIME_PATH, return_value=1000.0):
            stats.record_conn("1.1.1.1:8080", "example.com", 1)
            stats.record_conn("1.1.1.1:8080", "example.com", 1)
        self.assertEqual(stats.connection_count(), 2)
        entries = stats.live_connections()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["connections"], 2)
        self.assertEqual(entries[0]["first_seen"], 1000.0)
        with patch(_TIME_PATH, return_value=1005.0):
            stats.record_conn("1.1.1.1:8080", "example.com", -1)
        self.assertEqual(stats.connection_count(), 1)
        stats.record_conn("1.1.1.1:8080", "example.com", -1)
        # 归零即删（不保留余温），也不残留 first_seen
        self.assertEqual(stats.connection_count(), 0)
        self.assertEqual(stats.live_connections(), [])
        self.assertEqual(stats._conn_first_seen, {})

    def test_negative_clamped_to_zero(self):
        """异常路径下 -1 多于 +1 时钳到 0，不出现负计数"""
        stats, _ = _make()
        stats.record_conn("1.1.1.1:8080", "example.com", -1)
        self.assertEqual(stats.connection_count(), 0)
        self.assertEqual(stats.live_connections(), [])

    def test_empty_host_ignored(self):
        """空 host 的连接不计（与 pair 口径一致）"""
        stats, _ = _make()
        stats.record_conn("1.1.1.1:8080", "", 1)
        self.assertEqual(stats.connection_count(), 0)

    def test_live_connections_carry_target_ip(self):
        """实时条目的目标 IP 由探测缓存给出（未解析为 None）"""
        ip_map = {("1.1.1.1:8080", "example.com"): "93.184.216.34"}
        stats, _ = _make(ip_map=ip_map)
        stats.record_conn("1.1.1.1:8080", "example.com", 1)
        stats.record_conn("2.2.2.2:1080", "unresolved.example", 1)
        by_host = {e["host"]: e for e in stats.live_connections()}
        self.assertEqual(by_host["example.com"]["ip"], "93.184.216.34")
        self.assertIsNone(by_host["unresolved.example"]["ip"])


class TestHistoryPersistence(unittest.TestCase):
    """历史条目：增量累加落盘、IP 维度、节流、清理"""

    def test_flush_writes_incremental_deltas(self):
        """落盘写的是增量（store 侧累加），条目按 (bind, host, ip) 分区"""
        ip_map = {("1.1.1.1:8080", "example.com"): "93.184.216.34"}
        stats, store = _make(ip_map=ip_map)
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        stats.flush(1000.0, force=True)
        self.assertEqual(store.rows[("1.1.1.1:8080", "example.com", "93.184.216.34")]["upload"], 100)

        # 第二轮增量继续累加到同一行
        stats.record_bytes("1.1.1.1:8080", "example.com", 50, 0)
        stats.sample(1002.0)
        stats.flush(1002.0, force=True)
        row = store.rows[("1.1.1.1:8080", "example.com", "93.184.216.34")]
        self.assertEqual(row["upload"], 150)
        self.assertEqual(stats.history_count(), 1)

    def test_ip_change_creates_new_entry(self):
        """同一 (代理,域名) 解析出新 IP 时新增条目，旧条目冻结在切换前的累计值"""
        ip_map = {("1.1.1.1:8080", "example.com"): "1.1.1.1"}
        stats, store = _make(ip_map=ip_map)
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        stats.flush(1000.0, force=True)

        ip_map[("1.1.1.1:8080", "example.com")] = "2.2.2.2"
        stats.record_bytes("1.1.1.1:8080", "example.com", 50, 0)
        stats.sample(1010.0)
        stats.flush(1010.0, force=True)

        self.assertEqual(stats.history_count(), 2)
        self.assertEqual(store.rows[("1.1.1.1:8080", "example.com", "1.1.1.1")]["upload"], 100)
        self.assertEqual(store.rows[("1.1.1.1:8080", "example.com", "2.2.2.2")]["upload"], 50)

    def test_unresolved_ip_recorded_as_empty(self):
        """尚未解析出 IP 时条目 IP 记空串（条目本身照常落盘）"""
        stats, store = _make()
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 10, 0)
        stats.sample(1000.0)
        stats.flush(1000.0, force=True)
        self.assertIn(("1.1.1.1:8080", "example.com", ""), store.rows)

    def test_flush_throttled_until_due(self):
        """未到 flush_interval 不落盘，到点才写（force 不受节流约束）"""
        stats, store = _make(flush_interval=15.0)
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)          # 首次落盘（_last_flush 初始 0）
        self.assertEqual(stats.history_count(), 1)
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1005.0)          # 距上次 5s < 15s，不落盘
        self.assertEqual(store.rows[("1.1.1.1:8080", "example.com", "")]["upload"], 100)
        stats.flush(1005.0, force=True)
        self.assertEqual(store.rows[("1.1.1.1:8080", "example.com", "")]["upload"], 200)

    def test_flush_failure_keeps_pending(self):
        """写失败保留待落盘增量，下次重试"""
        class BrokenStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.fail = True

            def add_traffic_history(self, deltas):
                if self.fail:
                    raise RuntimeError("db down")
                super().add_traffic_history(deltas)

        store = BrokenStore()
        stats, _ = _make(store=store)
        stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        self.assertEqual(stats.history_count(), 0)
        store.fail = False
        stats.flush(1001.0, force=True)
        self.assertEqual(stats.history_count(), 1)

    def test_prune_removes_expired_entries(self):
        """按保留期清理历史条目（需跨过清理节流窗口）"""
        stats, store = _make(retention=100.0)
        with patch(_TIME_PATH, return_value=1000.0):
            stats.note_pair("1.1.1.1:8080", "example.com")
        stats.record_bytes("1.1.1.1:8080", "example.com", 100, 0)
        stats.sample(1000.0)
        self.assertEqual(stats.history_count(), 1)
        # 跨过 _PRUNE_INTERVAL：旧条目 last_seen=1000 已超保留期 → 删，新条目保留
        with patch(_TIME_PATH, return_value=10000.0):
            stats.note_pair("2.2.2.2:1080", "other.example")
        stats.record_bytes("2.2.2.2:1080", "other.example", 10, 0)
        stats.sample(10000.0)
        stats.flush(10000.0, force=True)
        self.assertEqual(set(store.rows), {("2.2.2.2:1080", "other.example", "")})

    def test_history_entries_sorted_by_total(self):
        """history_entries 按流量总计降序"""
        stats, _ = _make()
        stats.note_pair("1.1.1.1:8080", "small.example")
        stats.note_pair("2.2.2.2:1080", "big.example")
        stats.record_bytes("1.1.1.1:8080", "small.example", 10, 0)
        stats.record_bytes("2.2.2.2:1080", "big.example", 999, 0)
        stats.sample(1000.0)
        stats.flush(1000.0, force=True)
        entries = stats.history_entries(10)
        self.assertEqual([e["host"] for e in entries], ["big.example", "small.example"])
        self.assertEqual(stats.history_count(), 2)


if __name__ == "__main__":
    unittest.main()
