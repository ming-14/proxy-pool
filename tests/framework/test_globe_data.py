"""globe_data 纯函数测试：/api/globe 的目标弧聚合（(bind, 城市) 分组、hosts 排序截断、top N）

数据源为「活跃 pair + DNS 探测结果缓存」，纯函数不碰网络/存储。
"""

from __future__ import annotations

import unittest

from src.framework.web.globe_data import aggregate_flows

_BIND = "1.1.1.1:8080"


def _pair(host, *, bind=_BIND, rate=0.0):
    """active_pairs() 输出形状（只用到 bind/host/速率字段）"""
    return {"bind": bind, "host": host, "up": 0, "down": 0,
            "up_rate": rate, "down_rate": rate,
            "up_inst": rate, "down_inst": rate, "last_seen": 0.0}


def _row(cc="US", city="Ashburn", lat=39.0, lng=-77.5):
    return {"ip": "1.2.3.4", "country": "美国", "country_code": cc,
            "province": None, "city": city, "lat": lat, "lng": lng}


class TestAggregateFlows(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(aggregate_flows([], {}, 10), [])

    def test_groups_by_bind_and_city_summing_rates(self):
        pairs = [_pair("a.example", rate=10.0), _pair("b.example", rate=5.0)]
        rows = {(_BIND, "a.example"): _row(), (_BIND, "b.example"): _row()}
        flows = aggregate_flows(pairs, rows, 10)
        self.assertEqual(len(flows), 1)
        flow = flows[0]
        self.assertEqual(flow["bind"], _BIND)
        self.assertEqual(flow["up_rate"], 15.0)
        self.assertEqual(flow["down_rate"], 15.0)
        self.assertEqual(flow["up_inst"], 15.0)
        self.assertEqual(flow["hosts"], ["a.example", "b.example"])  # 按速率降序
        self.assertEqual((flow["lat"], flow["lng"]), (39.0, -77.5))

    def test_same_city_different_bind_stays_separate(self):
        pairs = [_pair("a.example"), _pair("a.example", bind="2.2.2.2:8080")]
        rows = {(_BIND, "a.example"): _row(), ("2.2.2.2:8080", "a.example"): _row()}
        flows = aggregate_flows(pairs, rows, 10)
        self.assertEqual(len(flows), 2)
        self.assertEqual({f["bind"] for f in flows}, {_BIND, "2.2.2.2:8080"})

    def test_rows_without_coordinates_produce_no_arc(self):
        pairs = [_pair("a.example"), _pair("b.example")]
        rows = {(_BIND, "a.example"): {"ip": "1.2.3.4", "lat": None, "lng": None},
                (_BIND, "b.example"): None}
        self.assertEqual(aggregate_flows(pairs, rows, 10), [])

    def test_hosts_capped_and_sorted(self):
        pairs = [_pair(f"h{i}.example", rate=float(i)) for i in range(10)]
        rows = {(_BIND, f"h{i}.example"): _row() for i in range(10)}
        flows = aggregate_flows(pairs, rows, 10)
        hosts = flows[0]["hosts"]
        self.assertEqual(len(hosts), 8)
        self.assertEqual(hosts[0], "h9.example")
        self.assertEqual(hosts[-1], "h2.example")  # 速率 0..9 的前 8 个：h9..h2

    def test_top_n_truncation_and_descending_order(self):
        pairs = [_pair("a.example", rate=1.0), _pair("a.example", bind="2.2.2.2:8080", rate=3.0),
                 _pair("a.example", bind="3.3.3.3:8080", rate=2.0)]
        rows = {(p["bind"], "a.example"): _row(city=f"City{p['bind'][0]}") for p in pairs}
        flows = aggregate_flows(pairs, rows, 2)
        self.assertEqual([f["bind"] for f in flows], ["2.2.2.2:8080", "3.3.3.3:8080"])
        self.assertEqual(flows[0]["up_rate"], 3.0)


if __name__ == "__main__":
    unittest.main()