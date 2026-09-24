"""AbuseIPDB 检查源（文档 https://docs.abuseipdb.com/#check-endpoint）

源特定信息集中在本模块：端点、字段映射、usage 词表、配置键（本节节名 = SOURCE.name）。
API key 由核心经环境变量注入（值来自 config/.env）；日限额 Standard 1000/天，面板按需
触发、不做批量轮询；结果由核心缓存（ippure.toml cache_ttl）避免重复消耗配额。
"""

from __future__ import annotations

import logging

from src.domain.purity import IppurePluginConfig, IppureSourceSpec, UsageType
from src.interfaces.ippure.base import HttpPurityChecker, compact

logger = logging.getLogger(__name__)

SOURCE = IppureSourceSpec(
    name="abuseipdb",
    env_keys=("ABUSEIPDB_API_KEY",),
    label="AbuseIPDB",
)

_API_URL = "https://api.abuseipdb.com/api/v2/check"
_ALLOWED_KEYS = frozenset({"max_age_in_days"})
_DEFAULT_MAX_AGE_DAYS = 90

# 源 usageType 原值 → 通用枚举（未收录的原值由 normalize 归为 unknown）
_USAGE_TYPES = {
    "Commercial": UsageType.BUSINESS,
    "Organization": UsageType.BUSINESS,
    "Government": UsageType.GOVERNMENT,
    "Military": UsageType.MILITARY,
    "University/College/School": UsageType.EDUCATION,
    "Library": UsageType.LIBRARY,
    "Content Delivery Network": UsageType.CDN,
    "Fixed Line ISP": UsageType.RESIDENTIAL,
    "Mobile ISP": UsageType.MOBILE,
    "Data Center/Web Hosting/Transit": UsageType.HOSTING,
    "Search Engine Spider": UsageType.SEARCH_ENGINE,
    "Reserved": UsageType.RESERVED,
}


def create_checker(config: IppurePluginConfig) -> AbuseIpDbChecker:
    """按插件配置构造检查器；配置键/取值非法直接抛错（启动失败）"""
    unknown = set(config.section) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"[abuseipdb] 含未知配置键 {sorted(unknown)}（可选 {sorted(_ALLOWED_KEYS)}）"
        )
    max_age = config.section.get("max_age_in_days", _DEFAULT_MAX_AGE_DAYS)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= 365:
        raise ValueError(f"[abuseipdb] max_age_in_days 非法 {max_age!r}（应为 1-365 整数）")
    return AbuseIpDbChecker(
        api_key=config.env["ABUSEIPDB_API_KEY"],
        max_age_in_days=max_age,
        timeout=config.timeout,
        proxy=config.proxy,
    )


class AbuseIpDbChecker(HttpPurityChecker):
    """AbuseIPDB /check：滥用置信度（0-100）+ 用途类型 + IP 元信息"""

    def __init__(self, *, api_key: str, max_age_in_days: int, timeout: float, proxy: str) -> None:
        super().__init__(timeout=timeout, proxy=proxy)
        self._api_key = api_key
        self._max_age_in_days = max_age_in_days

    def check(self, ip: str) -> dict:
        logger.info(
            "AbuseIPDB 查询: ip=%s maxAge=%d proxy=%s",
            ip, self._max_age_in_days, self._proxies is not None,
        )
        data = self._get_json(
            _API_URL,
            params={"ipAddress": ip, "maxAgeInDays": self._max_age_in_days},
            headers={"Accept": "application/json", "Key": self._api_key},
        ).get("data", {})
        return {
            "score": data.get("abuseConfidenceScore"),
            "usage_type": _USAGE_TYPES.get(data.get("usageType"), UsageType.UNKNOWN),
            "extra": compact({
                "country_code": data.get("countryCode"),
                "country_name": data.get("countryName"),
                "isp": data.get("isp"),
                "domain": data.get("domain"),
                "total_reports": data.get("totalReports"),
                "is_whitelisted": data.get("isWhitelisted"),
                "is_public": data.get("isPublic"),
                "ip_version": data.get("ipVersion"),
                "last_reported_at": data.get("lastReportedAt"),
            }),
        }
