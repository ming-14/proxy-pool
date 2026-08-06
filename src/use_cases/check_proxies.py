"""并发测试代理用例（原 scheduler.check_proxies_concurrent）

用并发调度器同时测试多个代理能否访问目标 URL，返回每个代理的结果。
async 实现：网络请求经 asyncio.to_thread 投递，serve 心跳等事件循环场景
不再被同步阻塞。
"""

import asyncio
import logging
import time
from typing import Callable

from src.domain.models import to_proxy_dict
from src.domain.ports import HttpGetter, TaskScheduler

logger = logging.getLogger(__name__)


class CheckProxiesUseCase:
    """并发测试多个代理能否访问 URL

    返回 {proxy: {"success": bool, "status_code": int|None, "elapsed": float, "error": str|None}}
    """

    def __init__(
        self,
        *,
        scheduler_factory: Callable[..., TaskScheduler],
        http_getter: HttpGetter,
    ) -> None:
        """
        Args:
            scheduler_factory: 并发调度器工厂（组合根注入 ProxyScheduler）
            http_getter:       HTTP 请求端口实现
        """
        self._scheduler_factory = scheduler_factory
        self._http_getter = http_getter

    async def execute(
        self,
        proxies: list[str],
        url: str,
        *,
        max_concurrent: int = 8,
        timeout: float = 10,
        headers: dict | None = None,
        on_result: Callable[[str, dict], None] | None = None,
    ) -> dict[str, dict]:
        """并发测试代理是否可访问 url

        返回 {proxy: {"success", "status_code", "elapsed", "error"}}。
        http/socks5 代理严格校验证书（verify=True），筛掉伪造证书的中间人代理；
        https 代理不校验（TLS 直连代理本身，自签证书校验会误杀，见 do_test）。

        Args:
            on_result: 每个代理探测完成即调（proxy, result），用于流式入队等实时处理；
                       None 则仅返回最终 results dict（批量语义）。
        """
        results: dict[str, dict] = {}

        async def do_test(proxy: str, _url: str) -> dict:
            proxy_dict = to_proxy_dict(proxy)
            t0 = time.time()
            # verify 语义按代理类型区分：
            #   http/socks5 代理 + https 目标走隧道端到端 TLS，verify 校验的是目标证书
            #   （防伪造证书的中间人代理），保留 True；
            #   https 代理的 TLS 直连代理本身，verify 校验的是代理自签证书，免费代理
            #   全部失败，须 False（与项目"免费代理多自签证书，校验会误杀"原则一致）。
            verify = not proxy.startswith("https://")
            try:
                resp = await asyncio.to_thread(
                    self._http_getter.get,
                    _url, timeout=timeout, headers=headers or {},
                    proxies=proxy_dict, verify=verify,
                )
                elapsed = time.time() - t0
                return {"success": True, "status_code": resp.status_code, "elapsed": elapsed, "error": None}
            except Exception as e:
                elapsed = time.time() - t0
                return {"success": False, "status_code": None, "elapsed": elapsed, "error": f"{type(e).__name__}: {e}"}

        tasks = [(p, url) for p in proxies]

        scheduler = self._scheduler_factory(
            proxies, max_concurrent=max_concurrent, max_fails=0, task_interval=0.1, request_timeout=timeout
        )

        def _on_success(p, t, r):
            results[p] = r
            if on_result is not None:
                on_result(p, r)

        await scheduler.run(tasks, "TestProxies", do_test, on_success=_on_success)

        for proxy in proxies:
            if proxy not in results:
                results[proxy] = {"success": False, "status_code": None, "elapsed": 0, "error": "未执行"}

        return results
