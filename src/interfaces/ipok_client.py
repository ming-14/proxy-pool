"""ipok.io IP 纯洁度检查客户端

接口适配器层。实现 domain.ports.IpPurityChecker，直连 ipok.io /api/ip 端点。
ipok.io 聚合多个源（ip-api / proxycheck / AbuseIPDB / ipapi.is / StopForumSpam /
IPOK-DB），返回综合 risk 分数（0-100）与 usageType（hosting/residential/mobile 等）。

无需 API key。max_age_in_days 参数 ipok 不支持（综合实时数据），接受但忽略。
"""

from __future__ import annotations

import logging

import requests
import urllib3

logger = logging.getLogger(__name__)

_API_URL = "https://ipok.io/api/ip"


class IpokClient:
    """ipok.io /api/ip 客户端：查询单个 IP 的综合风险分数与 usageType

    返回字段与 AbuseIpDbClient 对齐（snake_case），供 CheckPurityUseCase 统一合并。
    proxy 非空时通过代理访问（ipok.io 可能被墙）；代理证书常不匹配 IP，禁用验证。
    """

    def __init__(self, *, timeout: float = 10.0, proxy: str = ""):
        self._timeout = timeout
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        # 有代理时禁用 SSL 验证（代理证书常为自签/IP 证书不匹配）
        self._verify = not bool(proxy)
        if proxy:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def check(self, ip: str, *, max_age_in_days: int = 90) -> dict:
        """查询 IP 的综合风险分数与元信息

        Args:
            ip: IPv4 或 IPv6 地址（不含端口）
            max_age_in_days: ipok 不支持该参数（综合实时数据），接受但忽略
        Returns:
            {abuse_confidence_score, usage_type, country_code, country_name,
             isp, domain, total_reports, ...}
        Raises:
            requests.HTTPError: API 返回非 2xx
        """
        logger.info("ipok.io 查询: ip=%s proxy=%s", ip, self._proxies is not None)
        resp = requests.get(_API_URL, params={"ip": ip}, timeout=self._timeout,
                            proxies=self._proxies, verify=self._verify)
        resp.raise_for_status()
        data = resp.json()
        geo = data.get("geo", {})
        return {
            "abuse_confidence_score": data.get("risk"),
            "usage_type": data.get("usageType"),
            "country_code": geo.get("countryCode"),
            "country_name": geo.get("country"),
            "isp": geo.get("isp"),
            "domain": geo.get("org"),
            "total_reports": None,
            "is_whitelisted": None,
            "is_public": None,
            "ip_version": geo.get("version"),
            "last_reported_at": None,
        }
