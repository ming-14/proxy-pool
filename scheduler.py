import logging
import time
from concurrent.futures import ThreadPoolExecutor
import requests

logger = logging.getLogger(__name__)


class ProxyScheduler:
    """通用代理调度器：多线程并发执行任务，自动重试失败代理，超限则丢弃"""

    def __init__(self, proxies, max_concurrent=8, max_fails=2, task_interval=1.0, request_timeout=10):
        self._max_concurrent = max_concurrent
        self._max_fails = max_fails
        self._task_interval = task_interval
        self._request_timeout = request_timeout
        self._alive = set(proxies)
        self._failed = set()
        self._unused = list(proxies)
        self._next_time = {p: 0.0 for p in proxies}
        self._fails = {p: 0 for p in proxies}

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
        assignments = {}
        available = list(self._alive)
        for i, item in enumerate(items):
            if not available:
                break
            proxy = available[i % len(available)]
            assignments.setdefault(proxy, []).append(item)
        assigned = set(assignments.keys())
        self._unused = [p for p in self._unused if p not in assigned]
        unassigned = [item for item in items if not any(item in v for v in assignments.values())] if available else list(items)
        return assignments, unassigned

    def assign_sequential(self, items):
        assignments = {}
        unassigned = []
        for i, item in enumerate(items):
            if i < len(self._unused):
                proxy = self._unused[i]
                assignments.setdefault(proxy, []).append(item)
            else:
                unassigned.append(item)
        assigned = set(assignments.keys())
        self._unused = [p for p in self._unused if p not in assigned]
        return assignments, unassigned

    def run(self, tasks, phase_name, do_task, on_success=None):
        """执行任务列表

        tasks: [(proxy, task_data), ...] 每个任务绑定一个代理
        do_task: callable(proxy, task_data) -> result  执行单个任务，抛异常表示失败
        on_success: callable(proxy, task_data, result)  成功回调
        """
        results = {p: {"success": False, "failed_items": []} for p in self._alive}
        remaining = list(tasks)
        in_flight = {}

        with ThreadPoolExecutor(max_workers=self._max_concurrent) as executor:
            while remaining or in_flight:
                now = time.time()
                new_remaining = []

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

                    future = executor.submit(do_task, proxy, task_data)
                    in_flight[future] = task
                    self._next_time[proxy] = now + self._task_interval

                remaining = new_remaining

                if not in_flight:
                    if remaining:
                        time.sleep(0.5)
                    continue

                done = [f for f in in_flight if f.done()]
                if not done:
                    time.sleep(0.1)
                    continue

                for future in done:
                    task = in_flight.pop(future)
                    proxy, task_data = task
                    try:
                        result = future.result()
                        if proxy in results:
                            results[proxy]["success"] = True
                        if on_success:
                            on_success(proxy, task_data, result)
                        logger.info("[%s] %s 任务%s: 成功", phase_name, proxy, task_data)
                    except Exception as e:
                        self._fails[proxy] = self._fails.get(proxy, 0) + 1
                        reason = type(e).__name__
                        if self._fails[proxy] > self._max_fails:
                            self._alive.discard(proxy)
                            self._failed.add(proxy)
                            if proxy in results:
                                results[proxy]["failed_items"].append(task_data)
                            logger.warning("[%s] %s 任务%s: %s, 代理已丢弃", phase_name, proxy, task_data, reason)
                        else:
                            remaining.append(task)
                            logger.info("[%s] %s 任务%s: %s, 将重试", phase_name, proxy, task_data, reason)

        return results

    def _pick_proxy(self, task_id, in_flight):
        busy = set(v[1] for v in in_flight.values())
        candidates = [(p, self._next_time.get(p, 0.0)) for p in self._alive if p not in busy]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]


def request_with_retry(pm, url, max_retries=5, timeout=10, headers=None):
    """用代理池中的代理请求URL，自动重试换代理

    返回 (proxy, response) 或抛出最后一个异常
    """
    tried = set()
    last_exc = None

    for attempt in range(max_retries):
        proxy = pm.get_proxies(1, exclude=tried)
        if not proxy:
            break
        proxy = proxy[0]
        tried.add(proxy)
        proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        try:
            resp = requests.get(url, proxies=proxy_dict, timeout=timeout, headers=headers or {})
            pm.record_success(proxy)
            return proxy, resp
        except Exception as e:
            pm.record_fail(proxy)
            last_exc = e
            if pm.get_fail_count(proxy) >= 2:
                pm.remove(proxy)
                logger.info("代理 %s 失败次数过多, 已拉黑", proxy)
            logger.info("代理 %s 请求失败 (尝试 %d/%d): %s", proxy, attempt + 1, max_retries, e)

    if last_exc:
        raise last_exc
    raise RuntimeError("无可用代理")


def check_proxies_concurrent(proxies, url, max_concurrent=8, timeout=10, headers=None):
    """并发测试多个代理能否访问URL

    返回 {proxy: {"success": bool, "status_code": int|None, "elapsed": float, "error": str|None}}
    """
    results = {}

    def do_test(proxy, _url):
        proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        t0 = time.time()
        try:
            resp = requests.get(_url, proxies=proxy_dict, timeout=timeout, headers=headers or {})
            elapsed = time.time() - t0
            return {"success": True, "status_code": resp.status_code, "elapsed": elapsed, "error": None}
        except Exception as e:
            elapsed = time.time() - t0
            return {"success": False, "status_code": None, "elapsed": elapsed, "error": f"{type(e).__name__}: {e}"}

    tasks = [(p, url) for p in proxies]

    def on_success(proxy, task_data, result):
        results[proxy] = result

    def on_fail(proxy, task_data, result):
        results[proxy] = result

    sched = ProxyScheduler(proxies, max_concurrent=max_concurrent, max_fails=0, task_interval=0.1, request_timeout=timeout)

    def do_task(proxy, task_data):
        return do_test(proxy, task_data)

    sched_results = sched.run(tasks, "TestProxies", do_task, on_success=lambda p, t, r: results.update({p: r}))

    for proxy in proxies:
        if proxy not in results:
            results[proxy] = {"success": False, "status_code": None, "elapsed": 0, "error": "未执行"}

    return results
