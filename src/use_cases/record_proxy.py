"""代理记录用例（原 ProxyManager 的 record_success/record_fail/remove/trust）

记录成功/失败、拉黑、加入信任池等对池状态的变更。

consecutive_fails 为连续失败计数：失败 +1，任何成功清零（连续语义）。
"""

import logging

from src.domain.proxy_pool import ProxyPool

logger = logging.getLogger(__name__)


class RecordProxyUseCase:
    """记录代理成功/失败、移除拉黑、加入信任池"""

    def __init__(self, *, pool: ProxyPool, standby_group: str) -> None:
        """
        Args:
            pool:           代理池领域模型
            standby_group:  代理服务队列组名（serve 上游集合，成员成功后只复制不移动）
        """
        self._pool = pool
        self._standby_group = standby_group

    def record_success(self, proxy: str) -> None:
        """记录代理成功，将其加入信任池（连续失败计数清零）

        代理服务队列成员只复制进 trusted（保留在队列供 serve 上游使用），并清零
        其 standby 副本的连续失败计数（连续语义：任何成功都清零，避免瞬时
        失败永久累积）；其余组代理移动晋级。取代理时 get_available_proxies
        的 `proxy in result` 去重保证多组共存不会重复返回。
        """
        group_name, _ = self._pool.find_proxy(proxy)
        if group_name and group_name != self._pool.trusted_group:
            if group_name == self._standby_group:
                # 连续语义：清零队列副本的连续失败计数，再复制进 trusted（不掏空队列）
                self._pool.reset_consecutive_fails(proxy, group=self._standby_group)
                self._pool.add_to_group(self._pool.trusted_group, proxy, consecutive_fails=0)
            else:
                self._pool.move_proxy(proxy, self._pool.trusted_group, consecutive_fails=0)
            logger.info("代理 %s 已加入信任池", proxy)

    def record_fail(self, proxy: str, *, blacklist: bool = False) -> bool:
        """记录代理失败（连续失败 +1）；blacklist=True 时达阈值即拉黑移出，返回是否已拉黑

        "计数 → 达阈值 → 拉黑"的连续失败语义只在此实现，调用方不自行拼装这几步；
        锁定/固定代理的免疫属调用方策略，传 blacklist=False 只计数不移出。
        """
        _, info = self._pool.find_proxy(proxy)
        if info is None:
            logger.info("代理 %s 连续失败计数: 0", proxy)
            return False
        info["consecutive_fails"] += 1
        logger.info("代理 %s 连续失败计数: %d", proxy, info["consecutive_fails"])
        if blacklist and self.should_blacklist(proxy):
            self.remove(proxy)
            return True
        return False

    def remove(self, proxy: str) -> None:
        """移除代理所有组副本并加入黑名单组

        复制进 trusted 的代理服务队列代理被拉黑时，trusted 拦留副本一并清除，
        否则黑名单过期后残留副本会复活为可请求代理。
        """
        self._pool.remove_all_copies(proxy, self._pool.blacklist_group)

    def trust(self, proxy: str) -> None:
        """将代理加入信任池"""
        self._pool.add_to_group(self._pool.trusted_group, proxy, consecutive_fails=0)

    def get_consecutive_fail_count(self, proxy: str) -> int:
        return self._pool.get_consecutive_fail_count(proxy)

    def should_blacklist(self, proxy: str) -> bool:
        """代理连续失败次数是否达到所在组的拉黑阈值（trusted 容错更高，规则见 ProxyPool）"""
        return self._pool.should_blacklist(proxy)
