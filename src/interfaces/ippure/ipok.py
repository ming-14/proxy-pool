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

# usageType 词表：收录 ipok 响应契约里的全部取值
# （https://ipok.io/developers → isp | dual_isp | business | hosting | mobile |
#   education | government | unknown），另收 residential——该词按契约只出现在 ipType 里，
# 两个字段语义同源，同一个词落在哪个字段都按住宅处理。
# isp 是固网运营商、dual_isp 是双线运营商（同一地址被两家本土运营商判定为住宅归属），
# 二者在 ipok 的 ipType 里都归 residential，故同映射到住宅。
_USAGE_TYPES = {
    "isp": UsageType.RESIDENTIAL,
    "dual_isp": UsageType.RESIDENTIAL,
    "residential": UsageType.RESIDENTIAL,
    "mobile": UsageType.MOBILE,
    "hosting": UsageType.HOSTING,
    "business": UsageType.BUSINESS,
    "education": UsageType.EDUCATION,
    "government": UsageType.GOVERNMENT,
}

# ipType 词表：ipok 的粗分类，作为 usageType 为 unknown/缺失时的兜底
# （契约取值 residential | hosting | mobile | business | unknown）
_IP_TYPES = {
    "residential": UsageType.RESIDENTIAL,
    "mobile": UsageType.MOBILE,
    "hosting": UsageType.HOSTING,
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
        # 契约值是未知/缺失时退回 ipType 粗分类，两条都拿不到才归 unknown
        usage = (
            _USAGE_TYPES.get(data.get("usageType"))
            or _IP_TYPES.get(data.get("ipType"))
            or UsageType.UNKNOWN
        )
        return {
            "score": data.get("risk"),
            "usage_type": usage,
            "extra": compact({
                "country_code": geo.get("countryCode"),
                "country_name": geo.get("country"),
                "isp": geo.get("isp"),
                "domain": geo.get("org"),
                "ip_version": geo.get("version"),
            }),
        }
