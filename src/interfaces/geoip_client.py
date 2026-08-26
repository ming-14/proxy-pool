"""GeoLite2-City 离线 IP 归属地定位器（domain.ports.GeoLocator 实现）

接口适配器层。封装 geoip2 库读取 MaxMind GeoLite2-City mmdb，纯本地查询无网络 IO。
mmdb 文件由用户提供（vendor/geoip/GeoLite2-City.mmdb），定位结果供地图聚合用例使用。

名字多语言取中文优先（zh-CN），回退默认英文；经纬度取城市中心点坐标。
geoip2 Reader 支持多线程并发读（mmdb 只读内存映射），Web executor 线程调用安全。
"""

from __future__ import annotations

import logging

import geoip2.database
import geoip2.errors

logger = logging.getLogger(__name__)


def _pick_name(names: dict, fallback: str | None) -> str | None:
    """多语言名字取中文优先；无中文回退默认名（names.get 的 fallback 即默认语言名）"""
    if not names:
        return fallback
    return names.get("zh-CN") or fallback


class GeoLite2CityLocator:
    """GeoLite2-City 定位器：locate(ip) -> {country, country_code, province, city, lat, lng} | None"""

    def __init__(self, db_path: str) -> None:
        self._reader = geoip2.database.Reader(db_path)
        logger.info("GeoLite2-City 加载完成: %s", db_path)

    def locate(self, ip: str) -> dict | None:
        """查询 IP 归属；保留地址 / 库中无记录返回 None

        返回字段：country(中文国名), country_code(ISO 3166-1 两位码),
        province(省/州，可能 None), city(城市，可能 None), lat/lng(城市中心坐标)。
        """
        try:
            r = self._reader.city(ip)
        except (geoip2.errors.AddressNotFoundError, ValueError):
            # 保留地址（内网/本机/保留段）或非法的 IP 段在库中无记录
            return None
        except Exception:
            logger.exception("GeoLite2 查询异常 ip=%s", ip)
            return None

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
