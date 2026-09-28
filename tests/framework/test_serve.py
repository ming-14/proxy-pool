"""serve 层可独立测试的逻辑（不启动事件循环、不起服务器）"""

import unittest
from unittest.mock import MagicMock

from src.framework.serve import sync_retry_tasks


class TestSyncRetryTasks(unittest.TestCase):
    """补测任务表与待补测集合对齐：先回收已结束任务，再补启动"""

    @staticmethod
    def _task(done: bool) -> MagicMock:
        task = MagicMock()
        task.done.return_value = done
        return task

    def test_finished_task_replaced_in_same_round(self):
        """已结束的任务在同一轮被回收并为该代理重启，不占住 key 白等一轮"""
        retry_tasks = {"http://1.2.3.4:8080": self._task(done=True)}
        started = []
        sync_retry_tasks(retry_tasks, ["http://1.2.3.4:8080"], lambda p: started.append(p) or "new")
        self.assertEqual(started, ["http://1.2.3.4:8080"])
        self.assertEqual(retry_tasks, {"http://1.2.3.4:8080": "new"})

    def test_running_task_kept(self):
        """进行中的任务不重启也不回收"""
        running = self._task(done=False)
        retry_tasks = {"http://1.2.3.4:8080": running}
        started = []
        sync_retry_tasks(retry_tasks, ["http://1.2.3.4:8080"], lambda p: started.append(p))
        self.assertEqual(started, [])
        self.assertIs(retry_tasks["http://1.2.3.4:8080"], running)

    def test_new_pending_starts_task(self):
        """新出现的待补测代理启动任务"""
        retry_tasks = {}
        started = []
        sync_retry_tasks(retry_tasks, ["http://1.2.3.4:8080"], lambda p: started.append(p) or "t")
        self.assertEqual(started, ["http://1.2.3.4:8080"])
        self.assertEqual(retry_tasks, {"http://1.2.3.4:8080": "t"})

    def test_finished_task_dropped_when_no_longer_pending(self):
        """代理不再待补测时，已结束的任务被回收"""
        retry_tasks = {"http://1.2.3.4:8080": self._task(done=True)}
        sync_retry_tasks(retry_tasks, [], lambda p: "unused")
        self.assertEqual(retry_tasks, {})


if __name__ == "__main__":
    unittest.main()
