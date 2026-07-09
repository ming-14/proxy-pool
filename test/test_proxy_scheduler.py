import logging
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from scheduler import ProxyScheduler, request_with_retry, check_proxies_concurrent


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

    def test_assign_sequential(self):
        sched = ProxyScheduler(self._make_proxies(3))
        assignments, unassigned = sched.assign_sequential([2, 3, 4, 5])
        self.assertEqual(len(assignments), 3)
        self.assertEqual(len(unassigned), 1)
        self.assertEqual(unassigned, [5])

    def test_assign_sequential_more_pages_than_proxies(self):
        sched = ProxyScheduler(["1.1.1.1:8080"])
        assignments, unassigned = sched.assign_sequential([2, 3, 4])
        self.assertEqual(len(assignments), 1)
        self.assertEqual(len(unassigned), 2)

    def test_assign_sequential_empty_proxies(self):
        sched = ProxyScheduler([])
        assignments, unassigned = sched.assign_sequential([2, 3, 4])
        self.assertEqual(assignments, {})
        self.assertEqual(unassigned, [2, 3, 4])

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

    def test_assign_sequential_updates_unused(self):
        sched = ProxyScheduler(self._make_proxies(3))
        sched.assign_sequential([2, 3])
        self.assertEqual(len(sched.unused), 1)

    def test_assign_round_robin_updates_unused(self):
        sched = ProxyScheduler(self._make_proxies(3))
        sched.assign_round_robin([2, 3])
        self.assertEqual(len(sched.unused), 1)

    def test_run_success(self):
        sched = ProxyScheduler(["1.1.1.1:8080", "2.2.2.2:3128"], max_concurrent=2, max_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2), ("2.2.2.2:3128", 3)]
        results = sched.run(tasks, "Test", lambda proxy, task_data: "ok")
        self.assertTrue(any(r["success"] for r in results.values()))

    def test_run_failure_removes_proxy(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_fails=0, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        results = sched.run(tasks, "Test", lambda proxy, task_data: (_ for _ in ()).throw(Exception("fail")))
        self.assertIn("1.1.1.1:8080", sched.failed)
        self.assertNotIn("1.1.1.1:8080", sched.alive)

    def test_run_on_success_callback(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        collected = []

        def on_success(proxy, task_data, result):
            collected.append(task_data)

        sched.run(tasks, "Test", lambda proxy, task_data: "ok", on_success=on_success)
        self.assertEqual(len(collected), 1)

    def test_run_empty_tasks(self):
        sched = ProxyScheduler(self._make_proxies(3), max_concurrent=1, max_fails=1, task_interval=0)
        results = sched.run([], "Test", lambda proxy, task_data: "ok")
        self.assertEqual(results, {p: {"success": False, "failed_items": []} for p in sched.alive})

    def test_run_retry_on_fail(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_fails=2, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        call_count = 0

        def do_task(proxy, task_data):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise Exception("temp fail")
            return "ok"

        results = sched.run(tasks, "Test", do_task)
        self.assertTrue(results["1.1.1.1:8080"]["success"])

    def test_run_multiple_fails_removes_proxy(self):
        sched = ProxyScheduler(["1.1.1.1:8080"], max_concurrent=1, max_fails=1, task_interval=0)
        tasks = [("1.1.1.1:8080", 2)]
        results = sched.run(tasks, "Test", lambda proxy, task_data: (_ for _ in ()).throw(Exception("fail")))
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


class TestRequestWithRetry(unittest.TestCase):
    def test_success_on_first_try(self):
        pm = MagicMock()
        pm.get_proxies.return_value = ["1.1.1.1:8080"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("scheduler.requests.get", return_value=mock_resp):
            proxy, resp = request_with_retry(pm, "http://example.com")
        self.assertEqual(proxy, "1.1.1.1:8080")
        pm.record_success.assert_called_once_with("1.1.1.1:8080")

    def test_retry_on_failure(self):
        pm = MagicMock()
        pm.get_proxies.side_effect = [["1.1.1.1:8080"], ["2.2.2.2:3128"]]
        pm.get_fail_count.return_value = 1
        mock_resp = MagicMock()
        with patch("scheduler.requests.get", side_effect=[Exception("fail"), mock_resp]):
            proxy, resp = request_with_retry(pm, "http://example.com", max_retries=3)
        self.assertEqual(proxy, "2.2.2.2:3128")
        pm.record_fail.assert_called()

    def test_all_retries_exhausted(self):
        pm = MagicMock()
        pm.get_proxies.side_effect = [["1.1.1.1:8080"], ["2.2.2.2:3128"], []]
        pm.get_fail_count.return_value = 1
        with patch("scheduler.requests.get", side_effect=Exception("fail")):
            with self.assertRaises(Exception):
                request_with_retry(pm, "http://example.com", max_retries=3)

    def test_no_proxies_available(self):
        pm = MagicMock()
        pm.get_proxies.return_value = []
        with self.assertRaises(RuntimeError):
            request_with_retry(pm, "http://example.com")

    def test_blacklist_on_max_fails(self):
        pm = MagicMock()
        pm.get_proxies.side_effect = [["1.1.1.1:8080"], ["2.2.2.2:3128"]]
        pm.get_fail_count.return_value = 2
        mock_resp = MagicMock()
        with patch("scheduler.requests.get", side_effect=[Exception("fail"), mock_resp]):
            proxy, resp = request_with_retry(pm, "http://example.com", max_retries=3)
        pm.remove.assert_called_with("1.1.1.1:8080")


class TestCheckProxiesConcurrent(unittest.TestCase):
    def test_all_succeed(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("scheduler.requests.get", return_value=mock_resp):
            results = check_proxies_concurrent(["1.1.1.1:8080", "2.2.2.2:3128"], "http://example.com", max_concurrent=2)
        self.assertTrue(all(r["success"] for r in results.values()))
        self.assertEqual(len(results), 2)

    def test_all_fail(self):
        with patch("scheduler.requests.get", side_effect=Exception("fail")):
            results = check_proxies_concurrent(["1.1.1.1:8080"], "http://example.com", max_concurrent=1)
        self.assertFalse(results["1.1.1.1:8080"]["success"])
        self.assertIn("fail", results["1.1.1.1:8080"]["error"])

    def test_mixed_results(self):
        call_count = 0

        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("fail")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            return mock_resp

        with patch("scheduler.requests.get", side_effect=side_effect):
            results = check_proxies_concurrent(["1.1.1.1:8080", "2.2.2.2:3128"], "http://example.com", max_concurrent=2)
        successes = sum(1 for r in results.values() if r["success"])
        self.assertEqual(successes, 1)

    def test_empty_proxies(self):
        results = check_proxies_concurrent([], "http://example.com")
        self.assertEqual(results, {})


if __name__ == "__main__":
    unittest.main()
