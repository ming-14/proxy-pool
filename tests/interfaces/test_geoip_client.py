"""GeoLite2Locator 接口测试：用真实 mmdb 验证 City/Country 自动识别与输出字段"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.interfaces.geoip_client import GeoLite2Locator

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COUNTRY_DB = os.path.join(_ROOT, "vendor", "geoip", "GeoLite2-Country.mmdb")
_CITY_DB = os.path.join(_ROOT, "vendor", "geoip", "GeoLite2-City.mmdb")


class CountryLocatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_COUNTRY_DB):
            raise unittest.SkipTest("缺少 GeoLite2-Country.mmdb")
        cls.locator = GeoLite2Locator(_COUNTRY_DB)

    def test_country_returns_national_fields_only(self):
        """Country 库：仅国家级字段，省/市/坐标恒 None"""
        geo = self.locator.locate("8.8.8.8")
        assert geo is not None
        assert geo["country_code"] == "US"
        assert geo["province"] is None
        assert geo["city"] is None
        assert geo["lat"] is None and geo["lng"] is None

    def test_reserved_address_returns_none(self):
        """保留地址（内网段）无记录返回 None"""
        assert self.locator.locate("10.0.0.1") is None


class CityLocatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_CITY_DB):
            raise unittest.SkipTest("缺少 GeoLite2-City.mmdb")
        cls.locator = GeoLite2Locator(_CITY_DB)

    def test_city_returns_full_fields(self):
        """City 库：返回省/市/经纬度"""
        geo = self.locator.locate("8.8.8.8")
        assert geo is not None
        assert geo["country_code"] == "US"
        assert geo["lat"] is not None and geo["lng"] is not None

    def test_reserved_address_returns_none(self):
        assert self.locator.locate("10.0.0.1") is None


if __name__ == "__main__":
    unittest.main()
