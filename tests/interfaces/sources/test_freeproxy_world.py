"""FreeProxy.World 源测试：公共原语（_try_first_page/_crawl_pages）与多阶段编排

全程 mock 网络（_fetch_page / _try_first_page / _crawl_pages / _crawl_type），
不依赖外网，验证重试、直连回退、两轮爬取与 scheme 前缀入池逻辑。
"""

import asyncio
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
logging.disable(logging.WARNING)

from src.interfaces.sources import freeproxy_world as fpw
from src.interfaces.sources.freeproxy_world import (
    FpwConfig,
    FreeProxyWorldFetcher,
    _build_headers,
    _crawl_pages,
    _crawl_type,
    _fetch_page,
    _try_first_page,
)


class TestConfigInjection(unittest.TestCase):
    """FPW 参数经 create_source 构造注入（组合根传入 FpwConfig），模块不依赖框架层配置"""

    def test_create_source_injects_config(self):
        cfg = FpwConfig(max_concurrent=8, proxy_retry=3, task_interval=5,
                        request_timeout=7, accept_language="zh-CN")
        fetcher = fpw.create_source(config=cfg)
        self.assertIs(fetcher._config, cfg)

    def test_build_headers_uses_injected_accept_language(self):
        ua = MagicMock()
        ua.get_headers.return_value = {"User-Agent": "ua"}
        cfg = FpwConfig(accept_language="zh-CN,zh;q=0.9")
        headers = _build_headers(ua, "seed", cfg)
        self.assertEqual(headers["Accept-Language"], "zh-CN,zh;q=0.9")


class TestFetchPage(unittest.TestCase):
    """页面请求：run_in_executor 不转发 kwargs，参数须经 partial 透传（回归测试）"""

    PARSABLE = '<td style="font-weight: 500;">1.2.3.4</td><a href="/?port=8080">'

    def test_direct_branch(self):
        resp = MagicMock()
        resp.text = self.PARSABLE
        resp.raise_for_status.return_value = None
        with patch.object(fpw.http_client, "get", return_value=resp) as m_get:
            html, _ = asyncio.run(_fetch_page("http://u", {"A": "1"}, timeout=5))
        m_get.assert_called_once_with("http://u", headers={"A": "1"}, timeout=5)
        self.assertIn("1.2.3.4", html)

    def test_proxy_branch(self):
        resp = MagicMock()
        resp.text = self.PARSABLE
        resp.raise_for_status.return_value = None
        with patch.object(fpw.py_requests, "get", return_value=resp) as m_get:
            html, _ = asyncio.run(_fetch_page(
                "http://u", {"A": "1"}, {"https": "https://p1:80"}, timeout=5,
            ))
        m_get.assert_called_once_with(
            "http://u", headers={"A": "1"},
            proxies={"https": "https://p1:80"}, timeout=5, verify=False,
        )
        self.assertIn("1.2.3.4", html)


class TestTryFirstPage(unittest.TestCase):
    """第 1 页请求：重试直至成功，全失败返回 None"""

    def test_retries_then_success(self):
        calls = []

        async def fake_fetch(url, headers, proxies_dict=None, timeout=None, config=None):
            calls.append(1)
            if len(calls) < 3:
                raise Exception("net fail")
            return '<td style="font-weight: 500;">1.2.3.4</td><a href="/?port=8080">', 0.5

        with patch.object(fpw, "_fetch_page", side_effect=fake_fetch):
            result = asyncio.run(_try_first_page(
                "http://url", MagicMock(), None, "local", retries=3, timeout=5, phase="Phase1",
            ))
        self.assertEqual(result, (["1.2.3.4:8080"], 1))
        self.assertEqual(len(calls), 3)  # 两次失败后第三次成功

    def test_all_fail_returns_none(self):
        async def fake_fetch(url, headers, proxies_dict=None, timeout=None, config=None):
            raise Exception("net fail")

        with patch.object(fpw, "_fetch_page", side_effect=fake_fetch):
            result = asyncio.run(_try_first_page(
                "http://url", MagicMock(), None, "local", retries=2, timeout=5, phase="Phase1",
            ))
        self.assertIsNone(result)

    def test_invalid_page_raises_and_retries(self):
        """校验失败（ValueError，由 _fetch_page 内部抛出）与网络异常同等对待：重试直至成功"""
        calls = []

        async def fake_fetch(url, headers, proxies_dict=None, timeout=None, config=None):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("响应无有效数据(CF墙或空页)")
            return '<td style="font-weight: 500;">9.9.9.9</td><a href="/?port=80">', 0.3

        with patch.object(fpw, "_fetch_page", side_effect=fake_fetch):
            result = asyncio.run(_try_first_page(
                "http://url", MagicMock(), None, "local", retries=2, timeout=5, phase="Phase1",
            ))
        self.assertEqual(result[0], ["9.9.9.9:80"])


class TestCrawlPages(unittest.TestCase):
    """多线程爬取页面集合：两轮分配、失败重试/丢弃、结果收集"""

    def test_round_robin_assignment_succeeds(self):
        async def do_fetch(proxy, page):
            return "ok"

        pages = [2, 3, 4, 5]
        proxies = ["p1", "p2", "p3"]
        working, failed, unused = asyncio.run(_crawl_pages(
            proxies, pages, do_fetch, phase_name="Phase4-http",
        ))
        self.assertEqual(set(working), set(proxies))
        self.assertEqual(failed, [])
        # 轮询分配：3 代理 4 页 -> 全分配，无未使用代理
        self.assertEqual(unused, [])

    def test_failed_proxy_dropped_after_max_consecutive_fails(self):
        calls = []

        async def do_fetch(proxy, page):
            calls.append(proxy)
            if proxy == "p1":
                raise Exception("fail")
            return "ok"

        # 轮询分配：2 代理 3 页 -> p1:[2,4], p2:[3]；p1 连续失败 2 次丢弃
        working, failed, unused = asyncio.run(_crawl_pages(
            ["p1", "p2"], [2, 3, 4], do_fetch, phase_name="Phase4-http",
        ))
        self.assertNotIn("p1", working)
        self.assertIn("p2", working)
        self.assertIn("p1", failed)
        self.assertEqual(calls.count("p1"), 2)  # proxy_retry=1（默认配置）-> 失败 2 次丢弃

    def test_retry_unused_round(self):
        """失败页用未使用代理重爬一轮（Phase2 语义）"""
        async def do_fetch(proxy, page):
            if proxy == "p1":
                raise Exception("fail")
            return "ok"

        # 3 代理 2 页：p1:2, p2:3, p3 未使用；p1 失败 -> 页面 2 用 p3 重爬
        working, failed, unused = asyncio.run(_crawl_pages(
            ["p1", "p2", "p3"], [2, 3], do_fetch, phase_name="Phase2", retry_unused=True,
        ))
        self.assertIn("p3", working)  # 重试轮成功 -> 计入可用
        self.assertIn("p2", working)
        self.assertIn("p1", failed)
        # unused 为第一轮未分配代理（重试轮使用不影响其返回）
        self.assertEqual(unused, ["p3"])

    def test_on_page_callback_collects(self):
        collected = []

        async def do_fetch(proxy, page):
            return (f"html-{page}", 0.2)

        def on_page(proxy, page, result):
            html, _ = result
            collected.append(html)

        asyncio.run(_crawl_pages(
            ["p1"], [2], do_fetch, phase_name="Phase4-http", on_page=on_page,
        ))
        self.assertEqual(collected, ["html-2"])

    def test_empty_pages_returns_proxies_unused(self):
        async def do_fetch(proxy, page):
            return "ok"

        working, failed, unused = asyncio.run(_crawl_pages(
            ["p1", "p2"], [], do_fetch, phase_name="Phase2",
        ))
        self.assertEqual(working, [])
        self.assertEqual(failed, [])
        self.assertEqual(unused, ["p1", "p2"])


class TestCrawlType(unittest.TestCase):
    """类型爬取：中转代理逐个尝试 + 直连回退 + 后续页多线程"""

    def test_proxy_failure_falls_back_to_direct(self):
        """全部中转代理失败后回退直连（共用重试循环），再失败返回空"""
        ua = MagicMock()
        calls = []

        async def fake_first(url, ua_gen, proxies_dict, seed, retries, timeout, phase, config=None):
            calls.append((proxies_dict, seed))
            return None

        with patch.object(fpw, "_try_first_page", side_effect=fake_first):
            result = asyncio.run(_crawl_type(ua, ["w1", "w2"], ["u1"], "http", retries=2, timeout=5))
        # w1/w2 各试一次 + 直连回退一次，全部失败 -> 空结果
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2], (None, "local"))  # 直连回退 seed=local

    def test_crawl_type_full(self):
        """第 1 页成功 -> 后续页多线程爬取 -> 去重合并"""
        ua = MagicMock()
        first = (["1.1.1.1:8080"], 3)  # 第 1 页 3 页总数 -> 后续页 [2, 3]

        async def fake_first(url, ua_gen, proxies_dict, seed, retries, timeout, phase, config=None):
            return first

        crawled = []

        async def fake_crawl(proxies, pages, do_fetch, *, phase_name, on_page=None, retry_unused=False, config=None):
            crawled.append((list(proxies), list(pages), phase_name))
            # 模拟后续页解析出的代理（ip + 端口链接，与 _parse_proxies 正则一致）
            on_page("w1", 2, ('<td style="font-weight: 500;">2.2.2.2</td><a href="/?port=8080">', 0.1))
            return [], [], []

        with patch.object(fpw, "_try_first_page", side_effect=fake_first), \
             patch.object(fpw, "_crawl_pages", side_effect=fake_crawl):
            result = asyncio.run(_crawl_type(ua, ["w1", "w2"], ["u1"], "http", retries=2, timeout=5))

        self.assertEqual(crawled, [(["w1", "w2", "u1"], [2, 3], "Phase4-http")])
        # 第 1 页 + 后续页合并去重
        self.assertEqual(result, ["1.1.1.1:8080", "2.2.2.2:8080"])


class TestFetchProxiesFlow(unittest.TestCase):
    """顶层编排：Phase1-4 串联 + scheme 前缀 + 去重"""

    def test_full_flow_with_mocked_phases(self):
        async def fake_first(url, ua_gen, proxies_dict, seed, retries, timeout, phase, config=None):
            if phase == "Phase1":
                return (["h1", "h2"], 3)  # 中转代理 + 3 页
            return (["1.2.3.4:8080"], 1)

        async def fake_crawl(proxies, pages, do_fetch, *, phase_name, on_page=None, retry_unused=False, config=None):
            # Phase2：h1 可用，h2 未使用；failed 丢弃
            if phase_name == "Phase2":
                return ["h1"], ["h2"], ["h2"]
            return [], [], []

        async def fake_crawl_type(ua_gen, working, unused, proxy_type, retries, timeout, config=None):
            # 各类型第 1 页代理
            return [f"{proxy_type}-x1", f"{proxy_type}-x2"]

        with patch.object(fpw, "_try_first_page", side_effect=fake_first), \
             patch.object(fpw, "_crawl_pages", side_effect=fake_crawl), \
             patch.object(fpw, "_crawl_type", side_effect=fake_crawl_type):
            proxies = FreeProxyWorldFetcher().fetch_proxies(retries=1, timeout=5)

        expected = {
            "https://h1", "https://h2",  # 中转代理（working + unused）
            "http://http-x1", "http://http-x2",
            "https://https-x1", "https://https-x2",
            "socks5://socks5-x1", "socks5://socks5-x2",
        }
        self.assertEqual(set(proxies), expected)

    def test_phase1_failure_returns_empty(self):
        async def fake_first(url, ua_gen, proxies_dict, seed, retries, timeout, phase, config=None):
            return None

        with patch.object(fpw, "_try_first_page", side_effect=fake_first):
            proxies = FreeProxyWorldFetcher().fetch_proxies(retries=1, timeout=5)
        self.assertEqual(proxies, [])


if __name__ == "__main__":
    unittest.main()
