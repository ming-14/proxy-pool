"""serve 主循环 e2e 测试：真实 pproxy + mock 探测，验证完整稳定性

覆盖：
1. 启动补入（fill）填代理服务队列 → pproxy 可服务
2. 心跳循环稳定运行（不崩、不刷屏）
3. 队列耗尽后自动重补入
4. Ctrl+C / 取消后优雅退出（无 traceback）
5. 上游请求能实际经 pproxy 转发成功

全程本地 mock，不依赖外网。
"""

import asyncio
import inspect
import logging
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.WARNING)

from src.domain.proxy_pool import ProxyPool
from src.interfaces.proxy_server import PproxyServer
from src.use_cases.enqueue_filter import EnqueueFilterUseCase
from src.use_cases.record_proxy import RecordProxyUseCase
from src.framework import config
from src.framework.serve import _run
from src.use_cases.standby import StandbyQueueUseCase

from tests.e2e.mock_upstream import MOCK_BODY, start_mock_upstream


async def close_server(srv):
    """关闭 asyncio.Server（close 返回 None，非 awaitable）"""
    srv.close()
    await srv.wait_closed()


def make_pool():
    return ProxyPool(
        config.PROXY_GROUPS,
        max_consecutive_fails=config.MAX_CONSECUTIVE_FAILS,
        trusted_max_consecutive_fails=config.TRUSTED_MAX_CONSECUTIVE_FAILS,
        default_pause_ttl=config.SOURCE_PAUSE_TTL,
        requestable_groups=config.REQUESTABLE_GROUPS,
        trusted_group=config.TRUSTED_GROUP,
        blacklist_group=config.BLACKLIST_GROUP,
    )


def standby_list(pool):
    """代理服务队列组当前代理"""
    return list(pool.proxy_groups[config.STANDBY_GROUP]["proxies"].keys())


def make_standby(pool, record, check):
    """构造代理服务队列用例（容器壳：pool/record/check_proxies/save）

    e2e 用 mock 上游不走真实网络，链路门控注入恒可用的 mock。
    """
    gate = MagicMock()

    async def _gate_ok():
        return True

    gate.check.side_effect = _gate_ok
    return StandbyQueueUseCase(
        pool=pool,
        record=record,
        check_proxies=check,
        network_gate=gate,
        enqueue_filter=EnqueueFilterUseCase(rules=config.ENQUEUE_RULES),
        standby_group=config.STANDBY_GROUP,
        reject_group=config.ENQUEUE_REJECT_GROUP,
        fill_groups=config.STANDBY_FILL_GROUPS,
        fill_probe_multiplier=config.STANDBY_FILL_PROBE_MULTIPLIER,
        fill_probe_min_extra=config.STANDBY_FILL_PROBE_MIN_EXTRA,
        metrics_window=config.METRICS_WINDOW,
        chart_window=config.STANDBY_CHART_WINDOW,
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
                ret = on_result(p, results[p])
                if inspect.isawaitable(ret):
                    await ret
        return results


class _FakeServer:
    """serve._run 内的 PproxyServer 替身：记录 sync 调用与 sample_traffic 时刻（monotonic）"""

    def __init__(self, *args, **kwargs):
        self.sample_ts: list[float] = []
        self.sync_calls: list[tuple] = []  # 每次 sync 的 (proxies, unhealthy)

    def sync(self, proxies, unhealthy=()):
        self.sync_calls.append((list(proxies), set(unhealthy)))

    async def start(self, proxies):
        pass

    def sample_traffic(self):
        self.sample_ts.append(time.monotonic())

    async def close(self):
        pass


async def _wait_until(pred, timeout=5.0):
    """轮询等待条件满足（e2e 里 serve 内部循环异步启动）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("_wait_until 超时")


def make_min_container(pool, standby):
    """serve._run 的最小容器替身：真实 standby + 假 save/async_getter"""
    return SimpleNamespace(
        pool=pool,
        standby=standby,
        save=lambda: None,
        save_full=lambda: None,
        async_getter=AsyncMock(),
    )


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
            pool.add_to_group("default-http", dead1, consecutive_fails=config.MAX_CONSECUTIVE_FAILS - 1)  # 探测失败即达阈值拉黑
            pool.add_to_group("default-http", dead2, consecutive_fails=config.MAX_CONSECUTIVE_FAILS - 1)

            record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
            check = _FakeCheck([good])

            server = PproxyServer("http+socks4+socks5://127.0.0.1:0")
            await server.start([])

            # 1. 补入：good 入代理服务队列，死代理拉黑
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

            # 3. 心跳：good 存活保留在代理服务队列（不晋级 trusted）
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
                _cancel_test(c)
            )
            await asyncio.sleep(0.3)
            task.cancel()
            await asyncio.sleep(0.3)
            self.assertTrue(task.done(), "任务应已结束")

        asyncio.run(run())

    def test_locked_traffic_sampling_keeps_interval(self):
        """锁定单一上游后流量采样不随心跳放宽：仍按固定间隔（而非 30s 心跳间隔）采样

        回归：采样原挂在心跳轮次上，锁定时心跳放宽到 heartbeat_lock_interval，
        正被使用的锁定代理流量图跟着变稀疏。独立采样循环后锁定期间 ~2s/点。
        """
        async def run():
            upstream, mock_port = await start_mock_upstream()
            good = f"http://127.0.0.1:{mock_port}"
            pool = make_pool()
            pool.add_to_group("default-http", good)
            record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
            standby = make_standby(pool, record, _FakeCheck([good]))
            fake_server = _FakeServer()

            with patch("src.framework.serve.PproxyServer", return_value=fake_server):
                c = make_min_container(pool, standby)
                task = asyncio.create_task(
                    _run(c, interval=2, probe_url="http://probe.local", allowed_types={"http"},
                         concurrency=2, host="127.0.0.1", port=0, capacity=5, web_enabled=False)
                )
                await _wait_until(lambda: standby_list(pool) == [good])
                await asyncio.sleep(1.0)  # 首轮心跳完成，采样循环稳定运行

                standby.lock(good)
                lock_ts = time.monotonic()
                # 锁定时长 > 一个心跳轮次（heartbeat_lock_interval=30s）内：心跳不跑，
                # 采样应保持 ~2s 间隔继续产生点
                await asyncio.sleep(7.0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # 干净退出：采样循环在 cancel 时正常终止（CancelledError 传播，无其它异常）
                self.assertTrue(task.cancelled(), "serve._run 应被取消干净退出")

            after = [t for t in fake_server.sample_ts if t >= lock_ts]
            self.assertGreaterEqual(len(after), 2, "锁定期间流量采样应持续产生点")
            gaps = [b - a for a, b in zip(after, after[1:])]
            self.assertTrue(all(g < 5.0 for g in gaps), f"锁定期间采样间隔应 ~2s，实际 {gaps}")

            await close_server(upstream)

        asyncio.run(run())


class TestUpstreamHealthWiring(unittest.TestCase):
    """serve 主循环把心跳判定同步给 pproxy：失败代理随 sync 以 unhealthy 下发

    pproxy 的 `alive` 只由 CLI（`-a`）的 check_server_alive 翻转，本项目不用它——
    存活判定必须由 serve 每轮 sync 写入（否则调度过滤器形同虚设）。
    """

    def test_heartbeat_failure_synced_as_unhealthy(self):
        async def run():
            good = "http://127.0.0.1:1"
            flaky = "http://127.0.0.1:2"
            pool = make_pool()
            pool.add_to_group("default-http", good)
            pool.add_to_group("default-http", flaky)
            record = RecordProxyUseCase(pool=pool, standby_group=config.STANDBY_GROUP)
            check = _FakeCheck([good, flaky])   # 先都存活 → 两个都入队
            standby = make_standby(pool, record, check)
            fake_server = _FakeServer()

            with patch("src.framework.serve.PproxyServer", return_value=fake_server):
                c = make_min_container(pool, standby)
                task = asyncio.create_task(
                    _run(c, interval=0.2, probe_url="http://probe.local", allowed_types={"http"},
                         concurrency=2, host="127.0.0.1", port=0, capacity=5, web_enabled=False)
                )
                try:
                    await _wait_until(lambda: sorted(standby_list(pool)) == sorted([good, flaky]))
                    check.alive_proxies = {good}   # flaky 心跳失败 → 进入退避补测
                    await _wait_until(
                        lambda: any(flaky in unhealthy for _, unhealthy in fake_server.sync_calls))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            self.assertTrue(
                any(flaky in unhealthy for _, unhealthy in fake_server.sync_calls),
                f"失败代理未作为 unhealthy 下发给 pproxy: {fake_server.sync_calls}",
            )

        asyncio.run(run())


async def _cancel_test(c):
    """模拟 serve._run 的核心循环，验证取消能干净退出"""
    await c.fill({"http"}, "http://probe.local", timeout=3, concurrency=2, capacity=5)
    try:
        await asyncio.Future()  # 永久运行
    finally:
        pass


if __name__ == "__main__":
    unittest.main()
