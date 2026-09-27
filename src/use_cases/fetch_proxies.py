"""拉取代理用例（原 manager._fetch_proxies）

三段式：plan（选源，读池）/ collect（逐源拉取，纯网络，不碰池）/ apply（结果入池 + 源暂停）。
Web 面板把 collect 放 executor 线程、plan/apply 留在事件循环线程：代理池是纯内存可变
结构，池必须有固定单写线程——serve 心跳轮次（fill/heartbeat）在事件循环线程改池，若
拉取线程同时改池，会在对方 dict 迭代中变更（`for ... in group["proxies"].items()`）
直接抛 RuntimeError。CLI 等单线程路径用 execute() 串起三段即可（与 _do_purity 的
「网络放线程、状态回主线程」编排一致）。
"""

from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import urlsplit

from src.domain.models import SourceSpec
from src.domain.ports import ProxySource
from src.domain.proxy_pool import ProxyPool

logger = logging.getLogger(__name__)


class FetchProxiesUseCase:
    """从启用的代理源拉取代理，按组写入，并检查源去重率决定是否暂停"""

    def __init__(
        self,
        *,
        pool: ProxyPool,
        sources: list[SourceSpec],
        loader: Callable[[str], ProxySource],
        fetch_retries: int,
        dedup_threshold: float,
    ) -> None:
        """
        Args:
            pool:            代理池领域模型
            sources:         启用的代理源元数据列表
            loader:          按源名加载源对象（组合根提供 importlib 实现）
            fetch_retries:   每个源拉取时的重试次数
            dedup_threshold: 源去重率阈值，超过则暂停该源
        """
        self._pool = pool
        self._sources = sources
        self._loader = loader
        self._fetch_retries = fetch_retries
        self._dedup_threshold = dedup_threshold

    def execute(self, source_name: str | None = None) -> None:
        """同步执行三段（CLI / 池空自动拉取：单线程调用，无线程约束）"""
        self.apply(self.collect(self.plan(source_name)))

    def plan(self, source_name: str | None = None) -> list[SourceSpec]:
        """选出本轮要拉取的源（只读池；暂停源跳过并记日志）

        Args:
            source_name: 只拉取该源（面板单源拉取按钮）；None 表示拉取全部启用源
        """
        active_sources = [s for s in self._sources if s.enabled]
        if source_name is not None:
            active_sources = [s for s in active_sources if s.name == source_name]
            if not active_sources:
                logger.warning("单源拉取: 代理源 %s 不存在或未启用", source_name)
                return []
        sources_to_call = []
        for src in active_sources:
            if self._pool.is_source_paused(src.name):
                remaining = self._pool.source_pause_remaining(src.name)
                logger.info("代理源 %s 已暂停, 剩余 %ds", src.name, remaining)
                continue
            sources_to_call.append(src)
        logger.info("活跃代理源: %d/%d", len(sources_to_call), len(active_sources))
        return sources_to_call

    def collect(self, sources: list[SourceSpec]) -> list[tuple[SourceSpec, list[str] | None]]:
        """逐源拉取，返回 [(源, 代理列表)]（纯网络 + 源对象加载，不碰池）

        单源失败只记日志不中断其它源，该源以 None 返回——与「成功但无代理」的 []
        区分，调用方据此判断是否全部源都拉取失败（否则会把全失败报成拉取成功）。
        """
        results: list[tuple[SourceSpec, list[str] | None]] = []
        for src in sources:
            proxy_list: list[str] | None = None  # None = 该源拉取异常
            try:
                source = self._loader(src.name)
                proxy_list = source.fetch_proxies(retries=self._fetch_retries)
                logger.info("代理源 %s 返回 %d 个代理", src.name, len(proxy_list))
            except Exception as e:
                logger.warning("代理源 %s 失败: %s", src.name, e)
            results.append((src, proxy_list))
        return results

    def apply(self, results: list[tuple[SourceSpec, list[str] | None]]) -> None:
        """结果入池：源固定暂停 → 跨源去重入组 → 去重率过高暂停

        拉取失败的源（代理列表为 None）不入池、不参与去重率统计，但 pause_ttl 照常落地。
        """
        # 源自带 pause_ttl：拉过就暂停（不论成败），避免高频重复拉同一源
        for src, _ in results:
            if src.pause_ttl is not None:
                self._pool.pause_source(src.name, ttl=src.pause_ttl)
                logger.info("代理源 %s 已暂停 %d 小时", src.name, src.pause_ttl // 3600)

        new_count = 0
        seen = set()
        for src, proxy_list in results:
            if not proxy_list:  # None（拉取失败）与 []（成功但无代理）都不入池
                continue
            for p in proxy_list:
                if p in seen or self._pool.find_proxy(p)[0] is not None or self._pool.is_blacklisted(p):
                    continue
                seen.add(p)
                if _is_disabled_proxy(p):
                    # 禁用的代理（如端口 80 的假代理）直接入黑名单组，不进入可用池
                    self._pool.add_to_group(self._pool.blacklist_group, p)
                    continue
                self._pool.add_to_group(_resolve_group(src, p), p)
                new_count += 1
        available = self._pool.get_available_proxies()
        logger.info("去重后新增 %d 个代理 (可请求 %d, 信任池 %d, 黑名单 %d)",
                    new_count, len(available),
                    self._pool.get_group_size(self._pool.trusted_group),
                    self._pool.get_group_size(self._pool.blacklist_group))

        # 去重率过高则暂停源
        for src, proxy_list in results:
            if not proxy_list:
                continue
            known = sum(1 for p in proxy_list if self._pool.is_known_proxy(p))
            dedup_rate = known / len(proxy_list)
            if dedup_rate > self._dedup_threshold:
                self._pool.pause_source(src.name)
                logger.info("代理源 %s 去重率 %.0f%%, 暂停 %d 分钟",
                            src.name, dedup_rate * 100, self._pool.default_pause_ttl // 60)


def _resolve_group(src: SourceSpec, proxy: str) -> str:
    """计算代理入组名：group_by_protocol 源按 scheme 分拣为 group-scheme，否则固定 group"""
    if src.group_by_protocol and "://" in proxy:
        return f"{src.group}-{proxy.split('://', 1)[0]}"
    return src.group


# 禁用的代理端口：freeproxy-world 大量端口 80 的代理是 Cloudflare 假代理（CONNECT 400），
# 实测对外网不可用，拉取时直接入黑名单。
DISABLED_PORTS = {80}


def _is_disabled_proxy(proxy: str) -> bool:
    """判断代理是否命中禁用规则（端口在黑名单内）"""
    try:
        port = urlsplit(proxy if "://" in proxy else f"//{proxy}").port
    except ValueError:
        return False
    return port in DISABLED_PORTS
