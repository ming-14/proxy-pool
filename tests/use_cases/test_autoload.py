"""autoload 用例测试：清单解析（# 注释/空行/裸地址补 scheme）+ 入池（固定/跳过已存在/去重）"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.use_cases.autoload import AutoloadUseCase, _parse
from src.framework.config import (
    PROXY_GROUPS, MAX_CONSECUTIVE_FAILS, TRUSTED_MAX_CONSECUTIVE_FAILS,
    SOURCE_PAUSE_TTL, TRUSTED_GROUP, BLACKLIST_GROUP, STANDBY_GROUP,
)


def make_pool() -> ProxyPool:
    """构造默认配置的 ProxyPool（等价组合根的行为）"""
    return ProxyPool(
        PROXY_GROUPS,
        max_consecutive_fails=MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=SOURCE_PAUSE_TTL,
        requestable_groups=("default-http", "trusted", STANDBY_GROUP),
        trusted_group=TRUSTED_GROUP,
        blacklist_group=BLACKLIST_GROUP,
    )


class TestParse(unittest.TestCase):
    """_parse：清单格式解析（# 行内注释、空行、裸地址补 scheme、去重保序）"""

    def test_basic(self):
        self.assertEqual(_parse("http://1.2.3.4:8080"), ["http://1.2.3.4:8080"])

    def test_bare_addr_normalized(self):
        # 裸 ip:port 补默认 http:// scheme
        self.assertEqual(_parse("1.2.3.4:8080"), ["http://1.2.3.4:8080"])

    def test_unsupported_scheme_skipped(self):
        # 协议不在支持集合（socks4）：跳过该行，其余照常解析
        self.assertEqual(_parse("socks4://1.2.3.4:1080\nhttp://5.6.7.8:8080"),
                         ["http://5.6.7.8:8080"])

    def test_inline_comment(self):
        # # 行内注释：# 及之后忽略（含 # 前有空格的情况）
        self.assertEqual(_parse("http://1.2.3.4:8080 # 备注"), ["http://1.2.3.4:8080"])
        self.assertEqual(_parse("http://1.2.3.4:8080#备注"), ["http://1.2.3.4:8080"])

    def test_full_line_comment(self):
        # 整行为注释
        self.assertEqual(_parse("# 这是注释"), [])
        self.assertEqual(_parse("   # 缩进注释"), [])

    def test_blank_lines(self):
        self.assertEqual(_parse("\n\n   \n"), [])

    def test_multiple_lines(self):
        content = (
            "http://1.2.3.4:8080\n"
            "# 注释行\n"
            "\n"
            "socks5://5.6.7.8:1080 # socks5 代理\n"
            "9.10.11.12:3128\n"
        )
        self.assertEqual(_parse(content), [
            "http://1.2.3.4:8080",
            "socks5://5.6.7.8:1080",
            "http://9.10.11.12:3128",
        ])

    def test_dedup_preserves_order(self):
        # 文件内重复代理去重，保留首次出现顺序
        content = "http://1.2.3.4:8080\n1.2.3.4:8080\nhttp://5.6.7.8:1080"
        self.assertEqual(_parse(content), ["http://1.2.3.4:8080", "http://5.6.7.8:1080"])

    def test_empty_content(self):
        self.assertEqual(_parse(""), [])


class TestAutoloadUseCase(unittest.TestCase):
    """AutoloadUseCase.execute：入池 + 固定 + 跳过已存在"""

    def setUp(self):
        self.pool = make_pool()
        self.uc = AutoloadUseCase(pool=self.pool, standby_group=STANDBY_GROUP)

    def test_loads_into_standby_group(self):
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080")
        self.assertEqual(loaded, 1)
        self.assertEqual(skipped, 0)
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups[STANDBY_GROUP]["proxies"])

    def test_loaded_proxies_are_pinned(self):
        # autoload 代理标记 pinned（固定），排代理服务队列最前且不被踢出/拉黑
        self.uc.execute("http://1.2.3.4:8080\nhttp://5.6.7.8:1080")
        self.assertTrue(self.pool.is_pinned("http://1.2.3.4:8080"))
        self.assertTrue(self.pool.is_pinned("http://5.6.7.8:1080"))

    def test_skips_existing_in_any_group(self):
        # 已存在于任意组（含黑名单/信任池/代理服务队列）的代理跳过，不覆盖原状态
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080", consecutive_fails=3)
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080")
        self.assertEqual(loaded, 0)
        self.assertEqual(skipped, 1)
        # 原状态保留：仍在 trusted，consecutive_fails 不变
        self.assertIn("http://1.2.3.4:8080", self.pool.proxy_groups["trusted"]["proxies"])
        self.assertEqual(self.pool.proxy_groups["trusted"]["proxies"]["http://1.2.3.4:8080"]["consecutive_fails"], 3)

    def test_skips_existing_in_standby(self):
        # 已在代理服务队列（非固定）的代理跳过，不覆盖
        self.pool.add_to_group(STANDBY_GROUP, "http://1.2.3.4:8080", latency=0.5)
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080")
        self.assertEqual(loaded, 0)
        self.assertEqual(skipped, 1)
        # 未被标记 pinned（原状态保留）
        self.assertFalse(self.pool.is_pinned("http://1.2.3.4:8080"))

    def test_skips_blacklisted(self):
        # 黑名单代理跳过，不覆盖（不拉出黑名单）
        self.pool.add_to_group(BLACKLIST_GROUP, "http://1.2.3.4:8080")
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080")
        self.assertEqual(loaded, 0)
        self.assertEqual(skipped, 1)
        self.assertTrue(self.pool.is_blacklisted("http://1.2.3.4:8080"))

    def test_mixed_load_and_skip(self):
        self.pool.add_to_group("trusted", "http://1.2.3.4:8080")
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080\nhttp://5.6.7.8:1080")
        self.assertEqual(loaded, 1)
        self.assertEqual(skipped, 1)
        self.assertIn("http://5.6.7.8:1080", self.pool.proxy_groups[STANDBY_GROUP]["proxies"])

    def test_empty_content(self):
        loaded, skipped = self.uc.execute("")
        self.assertEqual((loaded, skipped), (0, 0))

    def test_idempotent(self):
        # 两次加载同一清单：第二次全部跳过（已存在）
        self.uc.execute("http://1.2.3.4:8080\nhttp://5.6.7.8:1080")
        loaded, skipped = self.uc.execute("http://1.2.3.4:8080\nhttp://5.6.7.8:1080")
        self.assertEqual(loaded, 0)
        self.assertEqual(skipped, 2)


if __name__ == "__main__":
    unittest.main()
