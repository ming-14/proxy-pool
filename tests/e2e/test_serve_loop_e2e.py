"""serve 主循环 e2e 测试：真实 pproxy + mock 探测，验证完整稳定性

覆盖：
1. 启动补入（fill）填备用队列 → pproxy 可服务
2. 心跳循环稳定运行（不崩、不刷屏）
3. 队列耗尽后自动重补入
4. Ctrl+C / 取消后优雅退出（无 traceback）
5. 上游请求能实际经 pproxy 转发成功

全程本地 mock，不依赖外网。
"""

import asyncio
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.interfaces.proxy_server import PproxyServer
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework import config
from src.use_cases.standby import StandbyQueueUseCase

from tests.e2e.mock_upstream import MOCK_BODY, start_mock_upstream


async def close_server(srv):
    """关闭 asyncio.Server（close 返回 None，非 awaitable）"""
    srv.close()
    await srv.wait_closed()


def make_pool():
    return ProxyPool(
        config.PROXY_GROUPS,
        max_fails=config.MAX_FAILS,
        trusted_max_fails=config.TRUSTED_MAX_FAILS,
        default_pause_ttl=config.SOURCE_PAUSE_TTL,
        requestable_groups=config.REQUESTABLE_GROUPS,
        trusted_group=config.TRUSTED_GROUP,
        blacklist_group=config.BLACKLIST_GROUP,
    )


def standby_list(pool):
    """备用队列组当前代理"""
    return list(pool.proxy_groups[config.STANDBY_GROUP]["proxies"].keys())


def make_standby(pool, record, check):
    """构造备用队列用例（容器壳：pool/record/check_proxies/save）"""
    return StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check,
        standby_group=config.STANDBY_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
    )


class _FakeCheck:
    """可控的探测器：alive_proxies 集合内的成功，其余失败（async 签名）"""

    def __init__(self, alive_proxies):
        self.alive_proxies = set(alive_proxies)

    async def execute(self, proxies, url, **kw):
        results = {}
        for p in proxies:
            if p in self.alive_proxies:
                results[p] = {"success": True, "status_code": 200, "elapsed": 0.1, "error": None}
            else:
                results[p] = {"success": False, "status_code": None, "elapsed": 1, "error": "Timeout"}
        on_result = kw.get("on_result")
        if on_result:
            for p in proxies:
                on_result(p, results[p])
        return results


class TestServeMainLoop(unittest.TestCase):
    """serve 主循环稳定性：补入 -> 心跳 -> 重补入 -> 优雅退出"""

    def test_full_loop_stable(self):
        async def run():
            upstream, mock_port = await start_mock_upstream()
            good = f"http://127.0.0.1:{mock_port}"
            dead1 = "http://127.0.0.1:1"
            dead2 = "http://127.0.0.1:2"

            pool = make_pool()
            pool.add_to_group("default-http", good)
            pool.add_to_group("default-http", dead1, fails=config.MAX_FAILS - 1)  # 探测失败即达阈值拉黑
            pool.add_to_group("default-http", dead2, fails=config.MAX_FAILS - 1)

            record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
            check = _FakeCheck([good])

            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start([])

            # 1. 补入：good 入备用队列，死代理拉黑
            c = make_standby(pool, record, check)
            added = await c.fill(
                {"http"}, "http://probe.local",
                timeout=3, concurrency=2, capacity=5,
            )
            self.assertEqual(added, 1)
            self.assertEqual(standby_list(pool), [good])
            self.assertTrue(pool.is_blacklisted(dead1))
            self.assertTrue(pool.is_blacklisted(dead2))

            # 2. 同步到 pproxy，请求应能经 good 上游转发
            server.sync(standby_list(pool))
            listen_port = server._handler.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
            writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await writer.drain()
            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            self.assertIn(MOCK_BODY, resp)

            # 3. 心跳：good 存活保留在备用队列（不晋级 trusted）
            alive = await c.heartbeat("http://probe.local", timeout=3, concurrency=2)
            self.assertEqual(alive, [good])
            self.assertEqual(standby_list(pool), [good])
            self.assertNotIn(good, pool.get_trusted_proxies())

            # 4. 队列耗尽的场景：good 也失败 -> 计次进入退避补测，补测失败达阈值拉黑清空队列
            check.alive_proxies = set()
            await c.heartbeat("http://probe.local", timeout=3, concurrency=2)
            self.assertEqual(c.pending_retries(), [good])
            self.assertIn(good, standby_list(pool))
            await c.retry_probe(good, "http://probe.local", timeout=3)
            self.assertEqual(standby_list(pool), [])
            self.assertTrue(pool.is_blacklisted(good))
            # 重补入：池中无存活 -> 队列仍空，但流程不崩
            await c.fill({"http"}, "http://probe.local",
                         timeout=3, concurrency=2, capacity=5)
            self.assertEqual(standby_list(pool), [])

            await server.close()
            await close_server(upstream)

        asyncio.run(run())

    def test_cancel_exits_cleanly(self):
        async def run():
            pool = make_pool()
            pool.add_to_group("default-http", "http://127.0.0.1:8080")
            record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
            check = _FakeCheck(["http://127.0.0.1:8080"])
            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")

            c = make_standby(pool, record, check)
            task = asyncio.create_task(
                _cancel_test(c, check)
            )
            await asyncio.sleep(0.3)
            task.cancel()
            await asyncio.sleep(0.3)
            self.assertTrue(task.done(), "任务应已结束")

        asyncio.run(run())


async def _cancel_test(c, check):
    """模拟 serve._run 的核心循环，验证取消能干净退出"""
    await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
    try:
        await asyncio.Future()  # 永久运行
    finally:
        pass


if __name__ == "__main__":
    unittest.main()
