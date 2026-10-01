"""检查 IP 纯净度用例：从代理地址提取 IP → 查缓存/调检查源 → 归一化 → 返回结果

用例层。协调 domain 模型（提取 IP）与端口（查询/缓存）并做结果归一化，不含网络细节，
也不认识任何具体检查源（源为插件，见 interfaces/ippure/）。

单源模式：ippure.toml [ippure].source 选中一个插件，组合根装配后注入本用例；本用例
不含多源合并逻辑。

缓存策略：结果持久化到 PurityCacheStore，按 (源名, IP) 分区——同一 IP 在不同源下的
分数不同，换源不得复用旧源结果；TTL 内直接返回缓存（from_cache=True），避免重复调 API
消耗日限额。TTL 判断在本层（缓存策略非领域规则）。

线程安全：SQLite 连接线程绑定，调网络 API 时不能碰 SQLite。故拆成三步：query_cache
（主线程 SQLite 读）→ check_remote（executor 纯网络）→ save_cache（主线程 SQLite 写）；
async resolve 是三步之上的统一编排入口（Web 面板与入队筛选共用）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.domain.models import proxy_to_ip
from src.domain.ports import IpPurityChecker, PurityCacheStore
from src.domain.purity import normalize_result

logger = logging.getLogger(__name__)


class CheckPurityUseCase:
    """检查代理纯净度：提取 IP → 查缓存 →（未命中）调检查源 → 写缓存 → 返回

    输入接受完整 proxy URL（http://1.2.3.4:8080）或裸 bind（1.2.3.4:8080），内部统一
    提取 IP 后调 checker。source 为缓存分区键，label 供面板标注数据来源。
    """

    def __init__(
        self,
        checker: IpPurityChecker,
        *,
        source: str,
        label: str,
        cache_ttl: float,
        cache: PurityCacheStore | None = None,
    ) -> None:
        self._checker = checker
        self._cache = cache
        self._cache_ttl = cache_ttl
        self.source = source
        self.label = label

    def query_cache(self, proxy: str) -> dict | None:
        """查缓存；命中返回完整结果（含 from_cache=True），未命中返回 None

        访问 SQLite，必须在创建连接的线程调用（Web 端为事件循环主线程）。
        """
        if self._cache is None:
            return None
        ip = proxy_to_ip(proxy)
        cached = self._cache.get_purity(self.source, ip)
        if cached is None or (time.time() - cached["cached_at"]) >= self._cache_ttl:
            return None
        result = dict(cached["result"])
        result["ip"] = ip
        result["from_cache"] = True
        logger.info("纯净度缓存命中: %s %s", self.source, ip)
        return result

    def check_remote(self, proxy: str) -> dict:
        """调检查源并归一化（纯网络，可放 executor 线程）；不碰缓存/SQLite"""
        ip = proxy_to_ip(proxy)
        logger.info("检查 IP 纯净度: %s (source=%s proxy=%s)", ip, self.source, proxy)
        result = normalize_result(self._checker.check(ip))
        result["ip"] = ip
        return result

    def save_cache(self, proxy: str, result: dict) -> dict:
        """写缓存并返回结果；访问 SQLite，必须在创建连接的线程调用"""
        if self._cache is not None:
            self._cache.save_purity(self.source, proxy_to_ip(proxy), result)
        return result

    # ------------------------------------------------------------------ 编排入口
    async def resolve(self, proxy: str) -> dict:
        """缓存优先的查询（Web 面板与入队筛选共用），命中直接返回（from_cache=True）"""
        cached = self.query_cache(proxy)
        if cached is not None:
            return cached
        return await self.inspect(proxy)

    async def inspect(self, proxy: str) -> dict:
        """调检查源并落缓存（异常原样抛出，由调用方决定展示/降级）

        线程安全：SQLite 连接线程绑定，故 check_remote 放 executor、save_cache 回
        调用线程（事件循环主线程）。
        """
        result = await asyncio.get_running_loop().run_in_executor(None, self.check_remote, proxy)
        return self.save_cache(proxy, result)

    async def resolve_score(self, proxy: str) -> int | None:
        """纯净度分数（入队筛选纯净度指标用）；源未给出/未测到返回 None（判定放过）

        normalize_result 已保证 score 为 0-100 整数或 None，此处不做二次校验。
        """
        return (await self.resolve(proxy)).get("score")
