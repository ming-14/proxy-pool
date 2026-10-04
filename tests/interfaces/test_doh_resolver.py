"""doh_resolver 的端点回退与响应解析测试

用记录调用的假 getter（只实现 get_json 契约）覆盖：端点顺序与回退、A/CNAME
混排解析、非 200/坏 JSON 视为端点失败、合法空结果不触发回退、去重与上限、
IDN punycode 编码、非法主机名不发起查询、证书策略按代理 scheme。
"""
import asyncio
import unittest

from src.interfaces.doh_resolver import DohResolver

_ENDPOINTS = [
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
    "https://223.5.5.5/resolve",
]

_HTTP_PROXY = "http://1.2.3.4:8080"


class FakeGetter:
    """按脚本返回/抛错的假 getter，记录每次调用参数

    results: list of (payload | Exception)，按调用顺序消费；超出脚本次数即断言失败，
    保证"多余调用"（例如负结果误触发回退）不会被静默吞掉。
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def get_json(self, url, *, timeout, headers=None, proxy=None, verify=False):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers,
                           "proxy": proxy, "verify": verify})
        if not self._results:
            raise AssertionError(f"get_json 调用次数超出测试脚本: {url}")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _resolve(host, results, proxy=_HTTP_PROXY, endpoints=None):
    getter = FakeGetter(results)
    resolver = DohResolver(endpoints or _ENDPOINTS, getter, timeout=7)
    addresses = asyncio.run(resolver.resolve(host, proxy=proxy))
    return addresses, getter


class TestDohResolver(unittest.TestCase):
    def test_first_endpoint_success_query_shape_and_a_records(self):
        """首个端点成功：只调一次；URL/头/代理/超时透传；CNAME 被忽略"""
        payload = {"Status": 0, "Answer": [
            {"name": "example.com", "type": 5, "data": "cdn.example.net."},
            {"name": "example.com", "type": 1, "data": "1.2.3.4"},
            {"name": "example.com", "type": 1, "data": "5.6.7.8"},
        ]}
        addresses, getter = _resolve("example.com", [payload])
        self.assertEqual(addresses, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(len(getter.calls), 1)
        call = getter.calls[0]
        self.assertEqual(call["url"],
                         "https://cloudflare-dns.com/dns-query?name=example.com&type=A")
        self.assertEqual(call["headers"], {"Accept": "application/dns-json"})
        self.assertEqual(call["proxy"], _HTTP_PROXY)
        self.assertEqual(call["timeout"], 7)
        self.assertIs(call["verify"], True)  # http 代理隧道端到端校验目标证书

    def test_fallback_across_endpoints(self):
        """前两个端点失败（非 200 / 坏 JSON）→ 第三个成功"""
        payload = {"Answer": [{"type": 1, "data": "9.9.9.9"}]}
        addresses, getter = _resolve(
            "example.com",
            [RuntimeError("HTTP 503"), ValueError("bad json"), payload],
        )
        self.assertEqual(addresses, ["9.9.9.9"])
        self.assertEqual([c["url"].split("?")[0] for c in getter.calls], _ENDPOINTS)

    def test_all_endpoints_fail_raises_last_error(self):
        errors = [RuntimeError("HTTP 500"), ValueError("bad json"), OSError("timed out")]
        getter = FakeGetter(errors)
        resolver = DohResolver(_ENDPOINTS, getter, timeout=1)
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(resolver.resolve("example.com", proxy=_HTTP_PROXY))
        self.assertEqual(len(getter.calls), 3)
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_valid_empty_answer_does_not_fall_back(self):
        """HTTP 200 合法 JSON 但无 A 记录 = 有效负结果：返回空列表且不试后续端点"""
        addresses, getter = _resolve(
            "nxdomain.example",
            [{"Status": 3, "Answer": []}, {"Answer": [{"type": 1, "data": "1.1.1.1"}]}],
        )
        self.assertEqual(addresses, [])
        self.assertEqual(len(getter.calls), 1)

    def test_non_object_payload_is_endpoint_failure(self):
        addresses, getter = _resolve(
            "example.com",
            [[], {"Answer": [{"type": 1, "data": "8.8.8.8"}]}],
        )
        self.assertEqual(addresses, ["8.8.8.8"])
        self.assertEqual(len(getter.calls), 2)

    def test_dedup_and_cap_at_three(self):
        payload = {"Answer": [
            {"type": 1, "data": "1.1.1.1"},
            {"type": 1, "data": "1.1.1.1"},
            {"type": 1, "data": "2.2.2.2"},
            {"type": 1, "data": "3.3.3.3"},
            {"type": 1, "data": "4.4.4.4"},
        ]}
        addresses, _ = _resolve("example.com", [payload])
        self.assertEqual(addresses, ["1.1.1.1", "2.2.2.2", "3.3.3.3"])

    def test_idn_host_is_punycode_encoded(self):
        payload = {"Answer": [{"type": 1, "data": "1.2.3.4"}]}
        addresses, getter = _resolve("例子.测试", [payload])
        self.assertEqual(addresses, ["1.2.3.4"])
        self.assertIn("name=xn--fsqu00a.xn--0zwm56d&type=A", getter.calls[0]["url"])

    def test_invalid_host_raises_without_query(self):
        getter = FakeGetter([])
        resolver = DohResolver(_ENDPOINTS, getter, timeout=1)
        with self.assertRaises(ValueError):
            asyncio.run(resolver.resolve("x" * 64 + ".example", proxy=_HTTP_PROXY))
        self.assertEqual(getter.calls, [])

    def test_https_proxy_skips_target_cert_verification(self):
        """https 代理的 TLS 只到代理自身（自签证书），校验会让免费代理全灭"""
        payload = {"Answer": [{"type": 1, "data": "1.2.3.4"}]}
        _, getter = _resolve("example.com", [payload], proxy="https://1.2.3.4:443")
        self.assertIs(getter.calls[0]["verify"], False)

    def test_socks_proxy_verifies_target(self):
        payload = {"Answer": [{"type": 1, "data": "1.2.3.4"}]}
        _, getter = _resolve("example.com", [payload], proxy="socks5://1.2.3.4:1080")
        self.assertIs(getter.calls[0]["verify"], True)


if __name__ == "__main__":
    unittest.main()
