"""IP 归属地图用例：收集池中全部 IP → 查缓存 → 缺失批量定位 → 写缓存 → 聚合地图点

用例层。协调 domain 模型（提取 IP、聚合）与端口（GeoLocator 定位 / GeoCacheStore 缓存），
不含 mmdb 读取细节。输出城市级散点 + 国家汇总，供 Web 面板 ECharts 地图展示。

缓存策略：定位结果持久化到 GeoCacheStore，TTL 内直接返回缓存，避免重复解析 mmdb；
TTL 判断在本层（缓存策略非领域规则）。

线程安全：SQLite 连接线程绑定，Web 端在 executor 线程调 mmdb（纯磁盘读）时不能碰 SQLite。
故拆成三步供 API 层编排：query_cache（主线程 SQLite 读）→ locate（executor 纯 mmdb）
→ save_cache（主线程 SQLite 写）。aggregate 为纯计算，任何线程可调。
"""

from __future__ import annotations

import logging
import time

from src.domain.models import proxy_to_ip
from src.domain.ports import GeoCacheStore, GeoLocator

logger = logging.getLogger(__name__)


class IpGeoUseCase:
    """IP 归属地图用例：缓存优先的批量定位 + 城市级聚合

    cache=None 时无缓存（每次全量重查 mmdb）。
    """

    def __init__(
        self,
        locator: GeoLocator,
        cache: GeoCacheStore | None = None,
        *,
        cache_ttl: float = 90 * 86400,
    ):
        self._locator = locator
        self._cache = cache
        self._cache_ttl = cache_ttl

    # ------------------------------------------------------------------ 步骤编排
    @staticmethod
    def collect_ips(pool) -> list[str]:
        """从池中收集全部不重复 IP（按出现顺序去重，纯内存操作）

        遍历所有组（含黑名单/锁定），跨组副本只计一次。
        """
        seen: set[str] = set()
        ips: list[str] = []
        for grp in pool.proxy_groups.values():
            for proxy in grp["proxies"]:
                ip = proxy_to_ip(proxy)
                if ip not in seen:
                    seen.add(ip)
                    ips.append(ip)
        return ips

    def query_cache(self, ips: list[str]) -> tuple[dict[str, dict], list[str]]:
        """查缓存：返回 (已缓存 {ip: geo}, 缺失待查列表)

        访问 SQLite，必须在创建连接的线程调用（Web 端为事件循环主线程）。
        缓存在 TTL 内的视为有效，过期视作缺失（由调用方重新定位覆盖）。
        """
        if not ips or self._cache is None:
            return {}, ips
        now = time.time()
        cached = self._cache.get_geo_batch(ips)
        valid: dict[str, dict] = {}
        missing: list[str] = []
        for ip in ips:
            geo = cached.get(ip)
            if geo is None or (now - geo["cached_at"]) >= self._cache_ttl:
                missing.append(ip)
            else:
                valid[ip] = geo
        return valid, missing

    def locate(self, ips: list[str]) -> dict[str, dict]:
        """批量定位缺失 IP（纯 mmdb 查询，可放 executor 线程）；不碰缓存/SQLite

        返回 {ip: {country, country_code, province, city, lat, lng}}，仅含定位成功的 IP。
        """
        results: dict[str, dict] = {}
        for ip in ips:
            geo = self._locator.locate(ip)
            if geo is not None:
                results[ip] = geo
        logger.info("IP 归属批量定位完成: 请求 %d, 成功 %d", len(ips), len(results))
        return results

    def save_cache(self, results: dict[str, dict]) -> None:
        """写缓存并返回结果；访问 SQLite，必须在创建连接的线程调用"""
        if self._cache is not None and results:
            self._cache.save_geo_batch(results)

    # ------------------------------------------------------------------ 聚合
    @staticmethod
    def aggregate(geo_map: dict[str, dict]) -> dict:
        """将 {ip: geo} 聚合为地图展示数据（纯计算）

        聚合键：城市存在用「国家+省+市」，无市用「国家+省」，再无省退「国家」；
        坐标取该组内首个 IP 的 lat/lng（同一城市中心坐标一致）。
        返回: {countries, points}
          countries: [{name, code, count}] 国家级汇总（按 count 降序）
          points:    [{name, country, country_code, lat, lng, count}] 城市级散点（count 降序）
        总数/解析数统计由 API 层组合（本方法不知池子总量）。
        """
        by_key: dict[tuple, dict] = {}
        for geo in geo_map.values():
            country = geo.get("country") or "未知"
            country_code = geo.get("country_code") or ""
            city = geo.get("city")
            province = geo.get("province")
            if city:
                key = (country_code, province or "", city)
                name = f"{province}{city}" if province else city
            elif province:
                key = (country_code, province, "")
                name = province
            else:
                key = (country_code, "", "")
                name = country
            entry = by_key.setdefault(
                key,
                {"name": name, "country": country, "country_code": country_code,
                 "lat": geo["lat"], "lng": geo["lng"], "count": 0},
            )
            entry["count"] += 1

        points = sorted(by_key.values(), key=lambda p: p["count"], reverse=True)
        countries: dict[str, dict] = {}
        for geo in geo_map.values():
            code = geo.get("country_code") or ""
            c = countries.setdefault(code, {"name": geo.get("country") or "未知", "code": code, "count": 0})
            c["count"] += 1
        country_list = sorted(countries.values(), key=lambda c: c["count"], reverse=True)
        return {"countries": country_list, "points": points}
