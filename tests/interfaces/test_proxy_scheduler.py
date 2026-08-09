import asyncio
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from src.interfaces.scheduler import ProxyScheduler


class TestProxyScheduler(unittest.TestCase):
    def test_init_alive(self):
        sched = ProxyScheduler(["1.1.1.1:8080", "2.2.2.2:3128", "3.3.3.3:80"])
        self.assertEqual(sched.alive, {"1.1.1.1:8080", "2.2.2.2:3128", "3.3.3.3:80"})
        self.assertEqual(sched.failed, set())

    def test_init_empty_proxies(self):
        sched = ProxyScheduler([])
        self.assertEqual(sched.alive, set())
        self.assertEqual(sched.failed, set())
        self.assertEqual(sched.unused, [])

    def test_assign_round_robin(self):
        sched = ProxyScheduler(self._make_proxies(3))
        assignments, _ = sched.assign_round_robin([2, 3, 4, 5, 6])
        total_pages = sum(len(v) for v in assignments.values())
        self.assertEqual(total_pages, 5)
        for proxy, pages in assignments.items():
            for p in pages:
                self.assertIn(p, [2, 3, 4, 5, 6])

    def test_assign_round_robin_uniform(self):
        sched = ProxyScheduler(self._make_proxies(3))
        assignments, _ = sched.assign_round_robin([2, 3, 4, 5, 6, 7])
        counts = [len(v) for v in assignments.values()]
        self.assertTrue(max(counts) - min(counts) <= 1)

    def test_assign_round_robin_empty_proxies(self):
        sched = ProxyScheduler([])
        assignments, unassigned = sched.assign_round_robin([2, 3, 4])
        self.assertEqual(assignments, {})
        self.assertEqual(unassigned, [2, 3, 4])

    def test_assign_round_robin_updates_unused(self):
        sched = ProxyScheduler(self._make_proxies(3))
        sched.assign_round_robin([2, 3])
        self.assertEqual(len(sched.unused), 1)

    def test_run_success(self):
        sched = ProxyScheduler(["1.1.1.1:8080", "2.2.2.2:3128"], max_concurrent=2, max_consecutive_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2), ("2.2.2.2:3128", 3)]

        async def do_task(proxy, task_data):
            return "ok"

        results = asyncio.run(sched.run(tasks, "Test", do_task))
        self.assertTrue(any(r["success"] for r in results.values()))

    def test_run_failure_removes_proxy(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_consecutive_fails=0, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]

        async def do_task(proxy, task_data):
            raise Exception("fail")

        results = asyncio.run(sched.run(tasks, "Test", do_task))
        self.assertIn("1.1.1.1:8080", sched.failed)
        self.assertNotIn("1.1.1.1:8080", sched.alive)

    def test_run_on_success_callback(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_consecutive_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        collected = []

        def on_success(proxy, task_data, result):
            collected.append(task_data)

        async def do_task(proxy, task_data):
            return "ok"

        asyncio.run(sched.run(tasks, "Test", do_task, on_success=on_success))
        self.assertEqual(len(collected), 1)

    def test_run_empty_tasks(self):
        sched = ProxyScheduler(self._make_proxies(3), max_concurrent=1, max_consecutive_fails=1, task_interval=0)

        async def do_task(proxy, task_data):
            return "ok"

        results = asyncio.run(sched.run([], "Test", do_task))
        self.assertEqual(results, {p: {"success": False, "failed_items": []} for p in sched.alive})

    def test_run_retry_on_fail(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_consecutive_fails=2, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        call_count = 0

        async def do_task(proxy, task_data):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise Exception("temp fail")
            return "ok"

        results = asyncio.run(sched.run(tasks, "Test", do_task))
        self.assertTrue(results["1.1.1.1:8080"]["success"])

    def test_run_multiple_consecutive_fails_removes_proxy(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_consecutive_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]

        async def do_task(proxy, task_data):
            raise Exception("fail")

        results = asyncio.run(sched.run(tasks, "Test", do_task))
        self.assertIn("1.1.1.1:8080", sched.failed)
        self.assertNotIn("1.1.1.1:8080", sched.alive)

    def test_alive_is_copy(self):
        sched = ProxyScheduler(self._make_proxies(3))
        alive = sched.alive
        alive.add("new_proxy")
        self.assertNotIn("new_proxy", sched.alive)

    def test_failed_is_copy(self):
        sched = ProxyScheduler(self._make_proxies(3))
        failed = sched.failed
        failed.add("new_proxy")
        self.assertNotIn("new_proxy", sched.failed)

    def _make_proxies(self, n):
        return [f"{i}.{i}.{i}.{i}:8080" for i in range(1, n + 1)]


if __name__ == "__main__":
    unittest.main()
