"""IP 归属地图用例：收集池中全部 IP → 查缓存 → 缺失批量定位 → 写缓存 → 聚合地图点

用例层。协调 domain 模型（提取 IP、聚合）与端口（GeoLocator 定位 / GeoCacheStore 缓存），
不含 mmdb 读取细节。输出城市级散点 + 国家汇总，供 Web 面板 ECharts 地图展示；
单代理国家查询（入队筛选国家指标）复用同一条缓存/定位路径。

缓存策略：定位结果持久化到 GeoCacheStore，TTL 内直接返回缓存，避免重复解析 mmdb；
TTL 判断在本层（缓存策略非领域规则）。

线程安全：SQLite 连接线程绑定，调 mmdb（纯磁盘读）时不能碰 SQLite。故底层拆成三步：
query_cache（主线程 SQLite 读）→ locate（executor 纯 mmdb）→ save_cache（主线程 SQLite 写）；
async resolve 是三者之上的统一编排入口（Web 面板与入队筛选共用）。aggregate 为纯计算，
任何线程可调。
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.domain.models import proxy_to_ip
from src.domain.ports import GeoCacheStore, GeoLocator

logger = logging.getLogger(__name__)

# Country 库无经纬度数据，散点坐标按 ISO 3166-1 alpha-2 国家中心坐标兜底
# （取质心或首都坐标，仅用于地图定位展示，非地理精确值）
_COUNTRY_CENTERS: dict[str, tuple[float, float]] = {
    "AF": (33.0, 65.0), "AL": (41.15, 20.17), "DZ": (28.03, 1.66), "AD": (42.5, 1.5),
    "AO": (-12.5, 18.5), "AG": (17.05, -61.8), "AR": (-34.0, -64.0), "AM": (40.07, 45.04),
    "AU": (-25.0, 135.0), "AT": (47.5, 14.5), "AZ": (40.14, 47.58), "BS": (24.25, -76.0),
    "BH": (26.03, 50.55), "BD": (23.68, 90.36), "BB": (13.1, -59.6), "BY": (53.7, 28.0),
    "BE": (50.5, 4.5), "BZ": (17.2, -88.7), "BJ": (9.3, 2.3), "BT": (27.5, 90.5),
    "BO": (-16.7, -64.7), "BA": (44.2, 17.8), "BW": (-24.7, 21.9), "BR": (-14.2, -51.9),
    "BN": (4.5, 114.7), "BG": (42.7, 25.5), "BF": (12.5, -1.7), "BI": (-3.5, 29.9),
    "KH": (12.5, 105.0), "CM": (7.3, 12.4), "CA": (56.1, -106.3), "CV": (16.0, -24.0),
    "CF": (6.6, 20.5), "TD": (15.4, 18.7), "CL": (-35.7, -71.5), "CN": (35.0, 105.0),
    "CO": (4.0, -73.0), "KM": (-12.2, 44.4), "CG": (-1.0, 15.5), "CD": (-4.0, 21.0),
    "CR": (9.7, -84.2), "CI": (8.0, -5.5), "HR": (45.1, 15.2), "CU": (21.5, -80.0),
    "CY": (35.1, 33.2), "CZ": (49.8, 15.5), "DK": (56.3, 9.5), "DJ": (11.8, 42.6),
    "DM": (15.4, -61.4), "DO": (19.0, -70.7), "EC": (-1.5, -78.5), "EG": (26.8, 30.8),
    "SV": (13.7, -88.9), "GQ": (1.7, 10.5), "ER": (15.2, 39.1), "EE": (58.6, 25.0),
    "SZ": (-26.5, 31.5), "ET": (9.1, 40.5), "FJ": (-17.7, 178.0), "FI": (61.9, 25.7),
    "FR": (46.6, 2.2), "GA": (-0.5, 11.8), "GM": (13.4, -15.4), "GE": (42.3, 43.4),
    "DE": (51.2, 10.4), "GH": (8.0, -1.0), "GR": (39.1, 21.8), "GD": (12.1, -61.7),
    "GT": (15.6, -90.3), "GN": (11.8, -10.9), "GW": (12.0, -15.0), "GY": (5.0, -58.9),
    "HT": (19.0, -72.8), "HN": (14.6, -86.3), "HU": (47.2, 19.5), "IS": (64.9, -19.0),
    "IN": (22.0, 79.0), "ID": (-2.0, 118.0), "IR": (32.4, 53.7), "IQ": (33.0, 43.7),
    "IE": (53.4, -8.2), "IL": (31.4, 35.0), "IT": (42.8, 12.8), "JM": (18.1, -77.3),
    "JP": (36.2, 138.25), "JO": (31.2, 36.9), "KZ": (48.0, 66.9), "KE": (0.0, 37.9),
    "KI": (1.9, -157.5), "KP": (40.0, 127.0), "KR": (36.5, 127.75), "KW": (29.3, 47.5),
    "KG": (41.2, 74.8), "LA": (18.0, 105.0), "LV": (56.9, 24.9), "LB": (33.9, 35.9),
    "LS": (-29.6, 28.2), "LR": (6.5, -9.5), "LY": (26.3, 17.2), "LI": (47.2, 9.5),
    "LT": (55.2, 24.0), "LU": (49.8, 6.1), "MG": (-19.4, 46.7), "MW": (-13.3, 34.3),
    "MY": (4.2, 102.0), "MV": (3.2, 73.2), "ML": (17.6, -4.0), "MT": (35.9, 14.4),
    "MH": (7.1, 171.2), "MR": (21.0, -10.9), "MU": (-20.3, 57.6), "MX": (23.6, -102.5),
    "FM": (6.9, 158.2), "MD": (47.4, 28.4), "MC": (43.7, 7.4), "MN": (46.8, 103.8),
    "ME": (42.8, 19.3), "MA": (32.7, -6.8), "MZ": (-18.7, 35.5), "MM": (21.0, 96.0),
    "NA": (-22.0, 17.0), "NR": (-0.5, 166.9), "NP": (28.2, 84.0), "NL": (52.3, 5.5),
    "NZ": (-41.5, 172.8), "NI": (12.9, -85.2), "NE": (17.6, 8.1), "NG": (9.1, 8.7),
    "MK": (41.6, 21.7), "NO": (60.5, 8.5), "OM": (21.0, 57.0), "PK": (30.4, 69.3),
    "PW": (7.5, 134.6), "PA": (8.5, -80.1), "PG": (-6.5, 143.5), "PY": (-23.4, -58.4),
    "PE": (-9.2, -75.0), "PH": (12.9, 121.8), "PL": (52.1, 19.4), "PT": (39.6, -8.0),
    "QA": (25.3, 51.2), "RO": (45.9, 25.0), "RU": (60.0, 100.0), "RW": (-1.9, 29.9),
    "KN": (17.3, -62.7), "LC": (13.9, -61.0), "VC": (13.3, -61.2), "WS": (-13.8, -172.1),
    "SM": (43.9, 12.4), "ST": (0.2, 6.6), "SA": (24.0, 45.0), "SN": (14.7, -14.5),
    "RS": (44.2, 20.9), "SC": (-4.7, 55.5), "SL": (8.6, -11.8), "SG": (1.35, 103.8),
    "SK": (48.7, 19.5), "SI": (46.1, 14.8), "SB": (-9.6, 160.2), "SO": (5.2, 46.2),
    "ZA": (-29.0, 25.0), "SS": (7.9, 30.3), "ES": (40.0, -3.7), "LK": (7.0, 81.0),
    "SD": (12.9, 30.2), "SR": (4.0, -56.0), "SE": (62.0, 15.0), "CH": (46.8, 8.2),
    "SY": (35.0, 38.0), "TW": (23.7, 121.0), "TJ": (38.9, 71.3), "TZ": (-6.4, 34.9),
    "TH": (15.0, 101.0), "TL": (-8.6, 125.9), "TG": (8.6, 0.8), "TO": (-21.2, -175.2),
    "TT": (10.7, -61.3), "TN": (33.9, 9.5), "TR": (39.0, 35.0), "TM": (38.9, 59.4),
    "TV": (-8.5, 179.2), "UG": (1.4, 32.3), "UA": (49.0, 31.0), "AE": (23.9, 54.3),
    "GB": (54.0, -2.0), "US": (39.8, -98.6), "UY": (-32.8, -56.0), "UZ": (41.4, 64.6),
    "VU": (-15.4, 166.9), "VA": (41.9, 12.45), "VE": (7.0, -65.0), "VN": (16.0, 108.0),
    "YE": (15.6, 47.8), "ZM": (-13.5, 27.8), "ZW": (-19.0, 29.8),
    # 地区/属地
    "HK": (22.3, 114.2), "MO": (22.2, 113.5), "PS": (31.9, 35.2), "GL": (71.7, -42.6),
    "PR": (18.2, -66.6), "GU": (13.4, 144.7), "VI": (18.3, -64.9), "VG": (18.4, -64.6),
    "AW": (12.5, -70.0), "CW": (12.2, -69.0), "BM": (32.3, -64.8), "GI": (36.1, -5.35),
    "FO": (61.9, -6.9), "IM": (54.2, -4.5), "JE": (49.2, -2.1), "GG": (49.5, -2.6),
    "NC": (-21.3, 165.5), "PF": (-17.7, -149.4), "CK": (-21.2, -159.8), "AI": (18.2, -63.1),
    "MS": (16.7, -62.2), "TC": (21.8, -71.8), "MP": (15.2, 145.7), "WF": (-13.3, -176.2),
    "NU": (-19.0, -169.9), "TK": (-9.2, -171.8), "PN": (-24.4, -127.7), "SH": (-15.9, -5.7),
    "YT": (-12.8, 45.2), "GF": (3.9, -53.0), "MQ": (14.6, -61.0), "GP": (16.3, -61.4),
    "RE": (-21.1, 55.5), "BL": (17.9, -62.8), "MF": (18.1, -63.1), "SX": (18.05, -63.05),
    "PM": (46.8, -56.2), "SJ": (77.9, 18.6), "AX": (60.2, 20.0), "EH": (24.2, -12.9),
    "IO": (-6.3, 71.8), "BV": (-54.4, 3.4), "HM": (-53.1, 73.5), "TF": (-49.2, 69.3),
    "NF": (-29.0, 167.9), "CX": (-10.5, 105.7), "CC": (-12.2, 96.9), "KY": (19.5, -80.5),
    "FK": (-51.8, -59.5), "GS": (-54.5, -37.0), "AS": (-14.3, -170.7), "UM": (19.3, 166.6),
}


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

    # ------------------------------------------------------------------ 编排入口
    async def resolve(self, ips: list[str], *, label: str) -> dict[str, dict]:
        """缓存优先的归属查询（Web 面板与入队筛选共用），返回 {ip: geo}

        三步各自降级，代价互不牵连：缓存读失败只能返回空；定位失败返回已读到的缓存
        （已缓存 IP 的归属照常下发，不因新 IP 定位失败连坐）；缓存写失败只丢持久化、
        不丢本次定位结果。本方法不外抛，调用方无需 try。
        """
        try:
            cached, missing = self.query_cache(ips)
        except Exception:
            logger.exception("IP 归属缓存读取失败 (%s): %d 个 IP", label, len(ips))
            return {}
        if not missing:
            return cached
        try:
            located = await asyncio.get_running_loop().run_in_executor(None, self.locate, missing)
        except Exception:
            logger.exception("IP 归属定位失败 (%s): %d 个 IP", label, len(missing))
            return cached
        try:
            self.save_cache(located)
        except Exception:
            logger.exception("IP 归属缓存写入失败 (%s): %d 个 IP", label, len(located))
        return {**cached, **located}

    async def resolve_country(self, proxy: str) -> str | None:
        """单个代理的国家代码（入队筛选国家指标用）；无记录/未定位到返回 None（判定放过）

        接受完整 proxy URL 或裸 bind，与 CheckPurityUseCase 同口径。
        """
        ip = proxy_to_ip(proxy)
        geo = await self.resolve([ip], label="enqueue")
        entry = geo.get(ip)
        return entry.get("country_code") if entry else None

    # ------------------------------------------------------------------ 聚合
    @staticmethod
    def aggregate(geo_map: dict[str, dict]) -> dict:
        """将 {ip: geo} 聚合为地图展示数据（纯计算）

        聚合键：城市存在用「国家+省+市」，无市用「国家+省」，再无省退「国家」；
        坐标取该组内首个 IP 的 lat/lng（同一城市中心坐标一致）。
        lat/lng 缺失时（Country 库无坐标）用国家中心坐标兜底，表中无记录的国家不进散点。
        返回: {countries, points}
          countries: [{name, code, count}] 国家级汇总（按 count 降序）
          points:    [{name, country, country_code, lat, lng, count}] 散点（count 降序）
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
            lat, lng = geo.get("lat"), geo.get("lng")
            if lat is None or lng is None:
                center = _COUNTRY_CENTERS.get(country_code)
                if center is None:
                    continue  # 无坐标国家不进散点，仅计入国家级汇总
                lat, lng = center
            entry = by_key.setdefault(
                key,
                {"name": name, "country": country, "country_code": country_code,
                 "lat": lat, "lng": lng, "count": 0},
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
