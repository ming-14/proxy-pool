"""出站协议描述表（proxy_scheme）测试：引擎 URI 映射 + 探测策略

协议知识集中在此表的 regression：映射/探测策略一旦改动，靠本文件定位。
needs_socks_connector 的行为断言在 test_http_client.py（与 ProxyConnector 用法同处）。
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.interfaces.proxy_scheme import engine_uri, verify_target


class TestEngineUri(unittest.TestCase):
    """池内代理字符串 → pproxy 上游 URI"""

    def test_http_kept(self):
        self.assertEqual(engine_uri("http://1.1.1.1:8080"), "http://1.1.1.1:8080")

    def test_socks5_kept(self):
        self.assertEqual(engine_uri("socks5://2.2.2.2:1080"), "socks5://2.2.2.2:1080")

    def test_https_converted_to_http_ssl(self):
        # https 代理（TLS 到代理）在 pproxy 中用 http+ssl:// 表示
        self.assertEqual(engine_uri("https://3.3.3.3:443"), "http+ssl://3.3.3.3:443")

    def test_socks4_kept_by_mechanism(self):
        # 策略已移除 socks4，但 pproxy 机制仍支持（vendor 用例直接以 socks4 作上游）
        self.assertEqual(engine_uri("socks4://4.4.4.4:1080"), "socks4://4.4.4.4:1080")

    def test_table_miss_passthrough(self):
        # 表外 scheme（pproxy 自己的 direct://）原样返回，不重建字符串
        self.assertEqual(engine_uri("direct://"), "direct://")


class TestVerifyTarget(unittest.TestCase):
    """探测时是否校验目标证书"""

    def test_tunnel_proxies_verify_target_cert(self):
        # http/socks 代理 + https 目标走隧道端到端 TLS，校验的是目标证书（可筛掉伪造证书的中间人代理）
        for proxy in ("http://1.1.1.1:8080", "socks5://2.2.2.2:1080", "socks4://4.4.4.4:1080"):
            self.assertTrue(verify_target(proxy), proxy)

    def test_https_proxy_skips_verification(self):
        # https 代理的 TLS 直连代理本身，校验的是代理自签证书（免费代理会全灭）
        self.assertFalse(verify_target("https://3.3.3.3:443"))

    def test_unknown_scheme_verifies(self):
        self.assertTrue(verify_target("foo://5.5.5.5:1080"))


if __name__ == "__main__":
    unittest.main()