"""MaxMind GeoLite2 离线 IP 归属地定位器（domain.ports.GeoLocator 实现）

接口适配器层。封装 geoip2 库读取 MaxMind GeoLite2 mmdb，纯本地查询无网络 IO。
mmdb 文件由用户提供（默认 vendor/geoip/），定位结果供地图聚合用例使用。

支持 City / Country 两种库，加载时按 mmdb metadata 的 database_type 自动识别：
- City（GeoLite2-City）：city() 查询，返回城市级字段（省/市/经纬度）
- Country（GeoLite2-Country）：country() 查询，仅国家级字段（省/市/经纬度恒为 None）

名字多语言取中文优先（zh-CN），回退默认英文；经纬度取城市中心点坐标。
geoip2 Reader 支持多线程并发读（mmdb 只读内存映射），Web executor 线程调用安全。
"""

from __future__ import annotations

import logging
import os

import geoip2.database
import geoip2.errors

logger = logging.getLogger(__name__)


def _pick_name(names: dict, fallback: str | None) -> str | None:
    """多语言名字取中文优先；无中文回退默认名（names.get 的 fallback 即默认语言名）"""
    if not names:
        return fallback
    return names.get("zh-CN") or fallback


class GeoLite2Locator:
    """GeoLite2 定位器：locate(ip) -> {country, country_code, province, city, lat, lng} | None

    City 库经纬度为城市中心坐标；Country 库 province/city/lat/lng 恒为 None
    （Country 库无坐标数据），坐标兜底由用例层国家中心表处理。
    """

    def __init__(self, db_path: str) -> None:
        self._reader = geoip2.database.Reader(db_path)
        self._metadata = self._reader.metadata()  # geoip2 5.x 的 metadata 为方法
        # 按库类型选择查询接口与结果字段
        self._is_city = "City" in self._metadata.database_type
        # 库指纹：文件名 + 构建版本，切换库时用于失效 geo 缓存
        self.fingerprint = f"{os.path.basename(db_path)}:{self._metadata.build_epoch}"
        logger.info("GeoLite2 加载完成: %s (%s)", db_path,
                    "City" if self._is_city else "Country")

    def locate(self, ip: str) -> dict | None:
        """查询 IP 归属；保留地址 / 库中无记录返回 None

        返回字段：country(中文国名), country_code(ISO 3166-1 两位码),
        province(省/州), city(城市), lat/lng(坐标)；Country 库后四者恒为 None。
        """
        try:
            if self._is_city:
                return self._locate_city(ip)
            return self._locate_country(ip)
        except (geoip2.errors.AddressNotFoundError, ValueError):
            # 保留地址（内网/本机/保留段）或非法的 IP 段在库中无记录
            return None
        except Exception:
            logger.exception("GeoLite2 查询异常 ip=%s", ip)
            return None

    def _locate_city(self, ip: str) -> dict:
        r = self._reader.city(ip)
        location = r.location
        if location.latitude is None or location.longitude is None:
            return None
        country_code = r.country.iso_code
        return {
            "country": _pick_name(r.country.names, r.country.name),
            "country_code": country_code,
            "province": _pick_name(r.subdivisions.most_specific.names, r.subdivisions.most_specific.name)
            if r.subdivisions.most_specific
            else None,
            "city": _pick_name(r.city.names, r.city.name) if r.city.name else None,
            "lat": location.latitude,
            "lng": location.longitude,
        }

    def _locate_country(self, ip: str) -> dict:
        # geoip2 5.x 的 Country response 无 location/subdivisions/city，仅国家级字段
        r = self._reader.country(ip)
        return {
            "country": _pick_name(r.country.names, r.country.name),
            "country_code": r.country.iso_code,
            "province": None,
            "city": None,
            "lat": None,
            "lng": None,
        }
