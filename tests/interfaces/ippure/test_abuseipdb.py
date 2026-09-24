"""AbuseIPDB 插件单测：元数据、字段映射、usage 归一化、配置校验、代理注入

requests.get 全程 mock，不打真实网络。
"""

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
logging.disable(logging.WARNING)

from src.domain.purity import IppurePluginConfig, UsageType  # noqa: E402
from src.interfaces.ippure import abuseipdb  # noqa: E402


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _config(**kwargs) -> IppurePluginConfig:
    base = dict(timeout=10.0, proxy="", section={}, env={"ABUSEIPDB_API_KEY": "k"})
    base.update(kwargs)
    return IppurePluginConfig(**base)


class SourceSpecTest(unittest.TestCase):
    def test_metadata(self):
        assert abuseipdb.SOURCE.name == "abuseipdb"
        assert abuseipdb.SOURCE.env_keys == ("ABUSEIPDB_API_KEY",)
        assert abuseipdb.SOURCE.label == "AbuseIPDB"


class CreateCheckerTest(unittest.TestCase):
    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            abuseipdb.create_checker(_config(section={"maxAge": 90}))
        assert "未知配置键" in str(ctx.exception)

    def test_default_and_custom_max_age(self):
        assert abuseipdb.create_checker(_config())._max_age_in_days == 90
        checker = abuseipdb.create_checker(_config(section={"max_age_in_days": 30}))
        assert checker._max_age_in_days == 30

    def test_invalid_max_age_raises(self):
        for bad in (0, 366, "90", True):
            with self.assertRaises(ValueError):
                abuseipdb.create_checker(_config(section={"max_age_in_days": bad}))


class CheckTest(unittest.TestCase):
    def test_field_mapping(self):
        payload = {"data": {
            "abuseConfidenceScore": 35,
            "usageType": "Fixed Line ISP",
            "countryCode": "DE",
            "countryName": "Germany",
            "isp": "TestISP",
            "domain": "test.example",
            "totalReports": 3,
            "isWhitelisted": False,
            "isPublic": True,
            "ipVersion": 4,
            "lastReportedAt": "2026-01-01T00:00:00+00:00",
        }}
        checker = abuseipdb.create_checker(_config())
        with patch("requests.get", return_value=_fake_response(payload)) as get:
            out = checker.check("1.2.3.4")
        assert out["score"] == 35
        assert out["usage_type"] is UsageType.RESIDENTIAL
        assert out["extra"] == {
            "country_code": "DE", "country_name": "Germany", "isp": "TestISP",
            "domain": "test.example", "total_reports": 3, "is_whitelisted": False,
            "is_public": True, "ip_version": 4,
            "last_reported_at": "2026-01-01T00:00:00+00:00",
        }
        # 请求参数：IP、maxAgeInDays、API key 头
        kwargs = get.call_args.kwargs
        assert kwargs["params"] == {"ipAddress": "1.2.3.4", "maxAgeInDays": 90}
        assert kwargs["headers"]["Key"] == "k"
        assert kwargs["timeout"] == 10.0

    def test_max_age_from_config_reaches_request(self):
        checker = abuseipdb.create_checker(_config(section={"max_age_in_days": 30}))
        with patch("requests.get", return_value=_fake_response({"data": {}})) as get:
            checker.check("1.2.3.4")
        assert get.call_args.kwargs["params"]["maxAgeInDays"] == 30

    def test_usage_type_vocabulary(self):
        cases = {
            "Commercial": UsageType.BUSINESS,
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
        for raw, expected in cases.items():
            with patch("requests.get", return_value=_fake_response(
                    {"data": {"usageType": raw}})):
                out = abuseipdb.create_checker(_config()).check("1.2.3.4")
            assert out["usage_type"] is expected, raw

    def test_unmapped_usage_type_falls_back_to_unknown(self):
        with patch("requests.get", return_value=_fake_response({"data": {"usageType": "??"}})):
            out = abuseipdb.create_checker(_config()).check("1.2.3.4")
        assert out["usage_type"] is UsageType.UNKNOWN

    def test_missing_data_key(self):
        with patch("requests.get", return_value=_fake_response({})):
            out = abuseipdb.create_checker(_config()).check("1.2.3.4")
        assert out["score"] is None
        assert out["extra"] == {}

    def test_http_error_propagates(self):
        resp = _fake_response({})
        resp.raise_for_status.side_effect = RuntimeError("401")
        with patch("requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                abuseipdb.create_checker(_config()).check("1.2.3.4")

    def test_proxy_disables_verification(self):
        checker = abuseipdb.create_checker(_config(proxy="http://127.0.0.1:7890"))
        with patch("requests.get", return_value=_fake_response({"data": {}})) as get:
            checker.check("1.2.3.4")
        kwargs = get.call_args.kwargs
        assert kwargs["proxies"] == {"http": "http://127.0.0.1:7890",
                                     "https": "http://127.0.0.1:7890"}
        assert kwargs["verify"] is False

    def test_direct_connection_verifies(self):
        checker = abuseipdb.create_checker(_config())
        with patch("requests.get", return_value=_fake_response({"data": {}})) as get:
            checker.check("1.2.3.4")
        assert get.call_args.kwargs["proxies"] is None
        assert get.call_args.kwargs["verify"] is True


if __name__ == "__main__":
    unittest.main()
