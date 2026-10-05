"""3D 地球展示数据的纯函数（web 适配层）：把活跃 pair 与 DNS 探测结果聚合成「代理→目标城市」弧

消费方只读「最终数据」——流量统计的活跃 pair + DNS 探测结果缓存，不感知探测队列本身。
"""

from __future__ import annotations

# 每个「代理→目标城市」分组最多带出的 host 数（tooltip 用）
_MAX_HOSTS_PER_GROUP = 8


def aggregate_flows(pairs: list[dict], target_rows: dict, max_flows: int) -> list[dict]:
    """把活跃 pair 与目标归属聚合成「代理→目标城市」弧数据（纯函数，可单测）

    pairs: active_pairs() 输出；target_rows: get_dns_probe_batch() 输出。
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