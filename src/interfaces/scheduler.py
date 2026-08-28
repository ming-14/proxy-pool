"""通用代理调度器：asyncio 并发执行任务，自动重试失败代理，超限则丢弃

实现 domain.ports.TaskScheduler 的结构签名（TestProxiesUseCase 只依赖 run）。
do_task 为 async 协程（网络请求自行 await asyncio.to_thread 等避免阻塞循环）。

max_consecutive_fails 为单次 run 内连续异常丢弃代理的阈值（调度器内部临时计数，
非持久化；任务异常 +1，成功不重置——单次 run 内连续异常才丢弃）。
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class ProxyScheduler:
    """通用代理调度器：asyncio 并发执行任务，自动重试失败代理，超限则丢弃"""

    def __init__(self, proxies, max_concurrent=8, max_consecutive_fails=2, task_interval=1.0, request_timeout=10):
        self._max_concurrent = max_concurrent
        self._max_consecutive_fails = max_consecutive_fails
        self._task_interval = task_interval
        self._request_timeout = request_timeout
        self._alive = set(proxies)
        self._failed = set()
        self._unused = list(proxies)
        self._next_time = {p: 0.0 for p in proxies}
        self._consecutive_fails = {p: 0 for p in proxies}

    @property
    def alive(self):
        return set(self._alive)

    @property
    def failed(self):
        return set(self._failed)

    @property
    def unused(self):
        return list(self._unused)

    def assign_round_robin(self, items):
        """轮询分配：第 i 个任务分给第 i % n 个未使用代理，每代理可分多任务

        按未使用代理的输入顺序轮询（确定性，不依赖 set 迭代序）；
        无未使用代理时全部任务未分配。
        """
        if not self._unused:
            return {}, list(items)
        assignments = {}
        for i, item in enumerate(items):
            proxy = self._unused[i % len(self._unused)]
            assignments.setdefault(proxy, []).append(item)
        assigned = set(assignments.keys())
        self._unused = [p for p in self._unused if p not in assigned]
        return assignments, []

    async def run(self, tasks, phase_name, do_task, on_success=None):
        """异步执行任务列表

        tasks: [(proxy, task_data), ...] 每个任务绑定一个代理
        do_task: async callable(proxy, task_data) -> result  执行单个任务，抛异常表示失败
        on_success: callable(proxy, task_data, result)  成功回调

        每轮循环：提交满足条件（并发上限 + 代理限流 + 存活）的任务，
        await 到至少一个完成再处理其结果（失败重试/超限丢弃）。
        """
        results = {p: {"success": False, "failed_items": []} for p in self._alive}
        remaining = list(tasks)
        in_flight = {}

        try:
            while remaining or in_flight:
                now = time.time()
                new_remaining = []

                # 提交本轮可执行任务（受并发上限与代理限流约束）
                for task in remaining:
                    if len(in_flight) >= self._max_concurrent:
                        new_remaining.append(task)
                        continue

                    proxy, task_data = task
                    if proxy not in self._alive:
                        continue
                    if self._next_time.get(proxy, 0) > now:
                        new_remaining.append(task)
                        continue

                    future = asyncio.create_task(do_task(proxy, task_data))
                    in_flight[future] = task
                    self._next_time[proxy] = now + self._task_interval

                remaining = new_remaining

                if not in_flight:
                    if remaining:
                        await asyncio.sleep(0.5)
                    continue

                done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)

                for future in done:
                    task = in_flight.pop(future)
                    proxy, task_data = task
                    try:
                        result = future.result()
                        # result 可能携带真实成败（如 {"success": bool}），以它为准
                        is_success = True
                        if isinstance(result, dict) and "success" in result:
                            is_success = bool(result["success"])
                        if proxy in results:
                            results[proxy]["success"] = is_success
                        if on_success:
                            on_success(proxy, task_data, result)
                        if is_success:
                            logger.debug("[%s] %s 任务%s: 成功", phase_name, proxy, task_data)
                        else:
                            logger.debug("[%s] %s 任务%s: 失败", phase_name, proxy, task_data)
                    except Exception as e:
                        self._consecutive_fails[proxy] = self._consecutive_fails.get(proxy, 0) + 1
                        reason = type(e).__name__
                        if self._consecutive_fails[proxy] > self._max_consecutive_fails:
                            self._alive.discard(proxy)
                            self._failed.add(proxy)
                            if proxy in results:
                                results[proxy]["failed_items"].append(task_data)
                            logger.warning("[%s] %s 任务%s: %s, 代理已丢弃", phase_name, proxy, task_data, reason)
                        else:
                            remaining.append(task)
                            logger.debug("[%s] %s 任务%s: %s, 将重试", phase_name, proxy, task_data, reason)
        except asyncio.CancelledError:
            # 调度器被取消（如 serve 退出）时，同步取消在途子任务避免泄漏
            for future in in_flight:
                future.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

        return results
