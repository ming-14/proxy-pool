"""测试用 PproxyServer 工厂：装配真实流量统计用例 + 空历史存储

serve e2e 只关心中转/同步行为，不需要历史落盘；统计用例本身是真实的（内存态），
这样 modstat 四条分支（字节/连接）走的是生产代码路径。
"""
from __future__ import annotations

from src.interfaces.proxy_server import PproxyServer
from src.use_cases.traffic_stats import TrafficStatsUseCase


class NullHistoryStore:
    """历史条目存储的空实现（TrafficHistoryStore 端口）"""

    def add_traffic_history(self, deltas):
        pass

    def get_traffic_history(self, limit):
        return []

    def count_traffic_history(self):
        return 0

    def prune_traffic_history(self, before_ts):
        return 0


def make_server(uri: str = "http+socks4+socks5://127.0.0.1:0") -> PproxyServer:
    """构造 PproxyServer，注入真实统计用例（内存态；历史落盘走空 store）"""
    traffic = TrafficStatsUseCase(
        store=NullHistoryStore(),
        target_ip_lookup=lambda pairs: {},
        metrics_window=1800.0,
        flush_interval=3600.0,
        retention=2592000.0,
    )
    return PproxyServer(uri, traffic=traffic)
