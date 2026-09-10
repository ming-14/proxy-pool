"""AbuseIPDB APIv2 客户端：查询 IP 滥用置信度与元信息

接口适配器层。实现 domain.ports.IpPurityChecker，直连 AbuseIPDB /check 端点
（不走代理，requests 直连）。API key 从 config/apikey.env 加载（见 framework.config）。

AbuseIPDB 文档：https://docs.abuseipdb.com/#check-endpoint
日限额：Standard 1000/天 / Basic 10000 / Premium 50000。调用方应按需检查
（Web 面板按钮触发），避免批量轮询耗尽配额。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CHECK_URL = "https://api.abuseipdb.com/api/v2/check"


class AbuseIpDbClient:
    """AbuseIPDB /check 客户端：查询单个 IP 的滥用置信度与元信息

    返回字段经 snake_case 转换，抹平 API 的 camelCase，供用例层与前端统一消费。
    proxy 非空时通过代理访问；代理证书常不匹配 IP，禁用验证。
    requests 方法级导入：客户端构造零成本，依赖链延迟到首次真实查询（面板按钮触发）。
    """

    def __init__(self, api_key: str, *, timeout: float = 10.0, proxy: str = ""):
        self._api_key = api_key
        self._timeout = timeout
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._verify = not bool(proxy)
        if proxy:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def check(self, ip: str, *, max_age_in_days: int = 90) -> dict:
        """查询 IP 的滥用置信度与元信息

        Args:
            ip: IPv4 或 IPv6 地址（不含端口）
            max_age_in_days: 只返回该天数内的举报（1-365，默认 90）
        Returns:
            {abuse_confidence_score, usage_type, country_code, country_name,
             isp, domain, total_reports, is_whitelisted, is_public, ip_version,
             last_reported_at}
        Raises:
            requests.HTTPError: API 返回非 2xx（如 401 key 失效 / 429 限额 / 422 参数错误）
        """
        headers = {"Accept": "application/json", "Key": self._api_key}
        params = {"ipAddress": ip, "maxAgeInDays": max_age_in_days}
        logger.info("AbuseIPDB 查询: ip=%s maxAge=%d proxy=%s", ip, max_age_in_days, self._proxies is not None)
        import requests
        resp = requests.get(_CHECK_URL, headers=headers, params=params, timeout=self._timeout,
                            proxies=self._proxies, verify=self._verify)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {
            "abuse_confidence_score": data.get("abuseConfidenceScore"),
            "usage_type": data.get("usageType"),
            "country_code": data.get("countryCode"),
            "country_name": data.get("countryName"),
            "isp": data.get("isp"),
            "domain": data.get("domain"),
            "total_reports": data.get("totalReports"),
            "is_whitelisted": data.get("isWhitelisted"),
            "is_public": data.get("isPublic"),
            "ip_version": data.get("ipVersion"),
            "last_reported_at": data.get("lastReportedAt"),
        }
