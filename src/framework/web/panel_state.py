"""面板任务状态注册表：测速 / 纯洁度检查的进行中与终态

Web 面板会话状态（内存），serve 重启清空：
- 测速：瞬时重操作结果，重启丢可接受（前端刷新后自然回落"未测速"）；
- 纯洁度：结果由 SQLite 缓存承载（30 天 TTL），本表只记 checking/error 中间态。

键设计：
- speedtests 按 bind（地址，不含 scheme）：测速针对具体代理；
- purity 按 proxy_to_ip 提取的 IP：同 IP 多端口/多协议行共享幂等，
  避免同一 IP 两行同时检查重复调 AbuseIPDB（有日限额）。

FIFO 容量上限：代理离队/拉黑后条目成孤儿（行不在快照即不展示），
但内存需有上限——超出优先淘汰最旧的非中间态条目，全中间态时淘汰最旧。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class PanelTasks:
    """测速与纯洁度检查的任务状态（进行中/终态）"""

    def __init__(self, capacity: int = 200) -> None:
        self._capacity = capacity
        self._speedtests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._purity: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # ---------------------------------------------------------------- 容量
    def _trim(self, table: OrderedDict[str, dict[str, Any]]) -> None:
        """FIFO 淘汰：优先淘汰最旧的非中间态条目；全中间态时淘汰最旧"""
        while len(table) > self._capacity:
            victim: str | None = None
            for k, v in table.items():
                if v.get("status") not in ("running", "checking"):
                    victim = k
                    break
            if victim is None:
                table.popitem(last=False)
            else:
                table.pop(victim)

    # ---------------------------------------------------------------- 测速
    def speedtest_start(self, bind: str) -> bool:
        """标记测速开始；已在 running 返回 False（幂等，不重复起任务）"""
        if self._speedtests.get(bind, {}).get("status") == "running":
            return False
        self._speedtests[bind] = {"status": "running", "result": None, "error": None, "ts": None}
        self._trim(self._speedtests)
        return True

    def speedtest_done(self, bind: str, result: dict) -> None:
        """测速完成：保存结果与完成时间戳（/api/status 下发供前端恢复）"""
        self._speedtests[bind] = {"status": "done", "result": result, "error": None, "ts": time.time()}

    def speedtest_error(self, bind: str, error: str) -> None:
        self._speedtests[bind] = {"status": "error", "result": None, "error": error, "ts": time.time()}

    def speedtest_of(self, bind: str) -> dict | None:
        return self._speedtests.get(bind)

    # ---------------------------------------------------------------- 纯洁度
    def purity_start(self, ip: str) -> bool:
        """标记检查开始；已在 checking 返回 False（幂等，防重复调 API 日限额）"""
        if self._purity.get(ip, {}).get("status") == "checking":
            return False
        self._purity[ip] = {"status": "checking", "error": None}
        self._trim(self._purity)
        return True

    def purity_done(self, ip: str) -> None:
        """检查完成：清除中间态（结果由 SQLite 缓存承载，快照读 row.purity）"""
        self._purity.pop(ip, None)

    def purity_error(self, ip: str, error: str) -> None:
        self._purity[ip] = {"status": "error", "error": error}

    def purity_of(self, ip: str) -> dict | None:
        return self._purity.get(ip)
