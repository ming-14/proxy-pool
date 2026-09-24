"""ipok 插件单测：元数据、字段映射、usage 归一化、配置校验、代理注入

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
from src.interfaces.ippure import ipok  # noqa: E402


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _config(**kwargs) -> IppurePluginConfig:
    base = dict(timeout=10.0, proxy="", section={}, env={})
    base.update(kwargs)
    return IppurePluginConfig(**base)


class SourceSpecTest(unittest.TestCase):
    def test_metadata(self):
        assert ipok.SOURCE.name == "ipok"
        assert ipok.SOURCE.env_keys == ()
        assert ipok.SOURCE.label == "ipok.io"


class CreateCheckerTest(unittest.TestCase):
    def test_no_params_allowed(self):
        assert ipok.create_checker(_config()) is not None

    def test_any_section_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            ipok.create_checker(_config(section={"timeout": 3}))
        assert "未知键" in str(ctx.exception)


class CheckTest(unittest.TestCase):
    def test_field_mapping(self):
        payload = {
            "risk": 12,
            "usageType": "residential",
            "geo": {"countryCode": "JP", "country": "Japan", "isp": "TestISP",
                    "org": "TEST-ORG", "version": 4},
        }
        with patch("requests.get", return_value=_fake_response(payload)) as get:
            out = ipok.create_checker(_config()).check("1.2.3.4")
        assert out["score"] == 12
        assert out["usage_type"] is UsageType.RESIDENTIAL
        assert out["extra"] == {"country_code": "JP", "country_name": "Japan",
                                "isp": "TestISP", "domain": "TEST-ORG", "ip_version": 4}
        assert get.call_args.kwargs["params"] == {"ip": "1.2.3.4"}

    def test_usage_type_vocabulary(self):
        cases = {
            "residential": UsageType.RESIDENTIAL,
            "mobile": UsageType.MOBILE,
            "hosting": UsageType.HOSTING,
            "business": UsageType.BUSINESS,
        }
        for raw, expected in cases.items():
            with patch("requests.get", return_value=_fake_response({"usageType": raw})):
                out = ipok.create_checker(_config()).check("1.2.3.4")
            assert out["usage_type"] is expected, raw

    def test_unmapped_usage_type_falls_back_to_unknown(self):
        with patch("requests.get", return_value=_fake_response({"usageType": "satellite"})):
            out = ipok.create_checker(_config()).check("1.2.3.4")
        assert out["usage_type"] is UsageType.UNKNOWN

    def test_missing_geo_is_tolerated(self):
        with patch("requests.get", return_value=_fake_response({"risk": 0, "geo": None})):
            out = ipok.create_checker(_config()).check("1.2.3.4")
        assert out["score"] == 0
        assert out["extra"] == {}

    def test_http_error_propagates(self):
        resp = _fake_response({})
        resp.raise_for_status.side_effect = RuntimeError("502")
        with patch("requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                ipok.create_checker(_config()).check("1.2.3.4")

    def test_proxy_disables_verification(self):
        checker = ipok.create_checker(_config(proxy="socks5://127.0.0.1:1080"))
        with patch("requests.get", return_value=_fake_response({})) as get:
            checker.check("1.2.3.4")
        kwargs = get.call_args.kwargs
        assert kwargs["proxies"] == {"http": "socks5://127.0.0.1:1080",
                                     "https": "socks5://127.0.0.1:1080"}
        assert kwargs["verify"] is False


if __name__ == "__main__":
    unittest.main()
