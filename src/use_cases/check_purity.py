"""检查代理纯洁度用例：从代理地址提取 IP → 查缓存/调 IpPurityChecker → 返回结果

用例层。协调 domain 模型（提取 IP）与端口（查询/缓存），不含网络细节。
Web 面板 / CLI 按需调用（AbuseIPDB 有日限额，不做批量轮询）。

单源模式：由 config.purity.source 决定使用哪个 checker（ipok / abuseipdb），
container 层组装。本用例只接受单个 checker，不含多源合并逻辑。

缓存策略：检查结果持久化到 PurityCacheStore，TTL 内直接返回缓存（from_cache=True），
避免重复调 API 消耗日限额。TTL 判断在本层（缓存策略非领域规则）。

线程安全：SQLite 连接线程绑定，Web 端在 executor 线程调网络 API 时不能碰 SQLite。
故拆成三步供 API 层编排：query_cache（主线程 SQLite 读）→ check_remote（executor
纯网络）→ save_cache（主线程 SQLite 写）。execute 保留给 CLI 同步场景（单线程）。
"""

from __future__ import annotations

import logging
import time

from src.domain.models import proxy_to_ip
from src.domain.ports import IpPurityChecker, PurityCacheStore

logger = logging.getLogger(__name__)


class CheckPurityUseCase:
    """检查代理纯洁度：提取 IP → 查缓存 →（未命中）调 checker → 写缓存 → 返回

    输入接受完整 proxy URL（http://1.2.3.4:8080）或裸 bind（1.2.3.4:8080），
    内部统一提取 IP 后调 checker。cache=None 时无缓存（每次都调 API）。
    """

    def __init__(
        self,
        checker: IpPurityChecker,
        cache: PurityCacheStore | None = None,
        *,
        max_age_in_days: int = 90,
        cache_ttl: float = 30 * 86400,
    ):
        self._checker = checker
        self._cache = cache
        self._max_age = max_age_in_days
        self._cache_ttl = cache_ttl

    def query_cache(self, proxy: str) -> dict | None:
        """查缓存；命中返回完整结果（含 from_cache=True），未命中返回 None

        访问 SQLite，必须在创建连接的线程调用（Web 端为事件循环主线程）。
        """
        if self._cache is None:
            return None
        ip = proxy_to_ip(proxy)
        cached = self._cache.get_purity(ip)
        if cached is None or (time.time() - cached["cached_at"]) >= self._cache_ttl:
            return None
        result = dict(cached["result"])
        result["ip"] = ip
        result["from_cache"] = True
        logger.info("纯洁度缓存命中: %s", ip)
        return result

    def check_remote(self, proxy: str) -> dict:
        """调 checker 查询（纯网络，可放 executor 线程）；不碰缓存/SQLite"""
        ip = proxy_to_ip(proxy)
        logger.info("检查 IP 纯洁度: %s (proxy=%s)", ip, proxy)
        result = self._checker.check(ip, max_age_in_days=self._max_age)
        result["ip"] = ip
        return result

    def save_cache(self, proxy: str, result: dict) -> dict:
        """写缓存并返回结果；访问 SQLite，必须在创建连接的线程调用"""
        if self._cache is not None:
            ip = proxy_to_ip(proxy)
            self._cache.save_purity(ip, result)
        return result

    def execute(self, proxy: str) -> dict:
        """同步执行全流程（CLI 等单线程场景）：查缓存 → 调 API → 写缓存

        Web 端不要调用此方法（SQLite 跨线程），改由 API 层编排
        query_cache → check_remote（executor）→ save_cache。
        """
        cached = self.query_cache(proxy)
        if cached is not None:
            return cached
        result = self.check_remote(proxy)
        return self.save_cache(proxy, result)
