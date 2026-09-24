"""ipok.io 检查源（直连 https://ipok.io/api/ip）

源特定信息集中在本模块：端点、字段映射、usage 词表、配置键（本节节名 = SOURCE.name）。
ipok 聚合多个上游（ip-api / proxycheck / AbuseIPDB / ipapi.is / StopForumSpam / IPOK-DB），
返回综合风险分与 usageType；无需 API key（env_keys 为空）。
"""

from __future__ import annotations

import logging

from src.domain.purity import IppurePluginConfig, IppureSourceSpec, UsageType
from src.interfaces.ippure.base import HttpPurityChecker, compact

logger = logging.getLogger(__name__)

SOURCE = IppureSourceSpec(
    name="ipok",
    env_keys=(),
    label="ipok.io",
)

_API_URL = "https://ipok.io/api/ip"
_ALLOWED_KEYS: frozenset[str] = frozenset()

# 源 usageType 原值 → 通用枚举（未收录的原值由 normalize 归为 unknown）
_USAGE_TYPES = {
    "hosting": UsageType.HOSTING,
    "residential": UsageType.RESIDENTIAL,
    "mobile": UsageType.MOBILE,
    "business": UsageType.BUSINESS,
}


def create_checker(config: IppurePluginConfig) -> IpokChecker:
    """按插件配置构造检查器；本源无参数，出现任何配置键即抛错（启动失败）"""
    unknown = set(config.section) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"[ipok] 不含任何配置键，出现未知键 {sorted(unknown)}")
    return IpokChecker(timeout=config.timeout, proxy=config.proxy)


class IpokChecker(HttpPurityChecker):
    """ipok.io /api/ip：综合风险分（0-100）+ 用途类型 + IP 元信息"""

    def check(self, ip: str) -> dict:
        logger.info("ipok.io 查询: ip=%s proxy=%s", ip, self._proxies is not None)
        data = self._get_json(_API_URL, params={"ip": ip})
        geo = data.get("geo") or {}
        return {
            "score": data.get("risk"),
            "usage_type": _USAGE_TYPES.get(data.get("usageType"), UsageType.UNKNOWN),
            "extra": compact({
                "country_code": geo.get("countryCode"),
                "country_name": geo.get("country"),
                "isp": geo.get("isp"),
                "domain": geo.get("org"),
                "ip_version": geo.get("version"),
            }),
        }
