"""拉取代理用例（原 manager._fetch_proxies）

协调各代理源拉取 -> 跨源去重入组 -> 源去重率过高暂停。
"""

from __future__ import annotations

import logging
import time
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
        """
        Args:
            source_name: 只拉取该源（面板单源拉取按钮）；None 表示拉取全部启用源
        """
        active_sources = [s for s in self._sources if s.enabled]
        if source_name is not None:
            active_sources = [s for s in active_sources if s.name == source_name]
            if not active_sources:
                logger.warning("单源拉取: 代理源 %s 不存在或未启用", source_name)
                return
        sources_to_call = []
        for src in active_sources:
            if self._pool.is_source_paused(src.name):
                remaining = self._pool.source_pause_remaining(src.name)
                logger.info("代理源 %s 已暂停, 剩余 %ds", src.name, remaining)
                continue
            sources_to_call.append(src)
        logger.info("活跃代理源: %d/%d", len(sources_to_call), len(active_sources))

        # 第一阶段：逐源拉取
        source_results: dict[str, tuple[list[str], SourceSpec]] = {}
        for src in sources_to_call:
            try:
                source = self._loader(src.name)
                proxy_list = source.fetch_proxies(retries=self._fetch_retries)
                logger.info("代理源 %s 返回 %d 个代理", src.name, len(proxy_list))
                source_results[src.name] = (proxy_list, src)
            except Exception as e:
                logger.warning("代理源 %s 失败: %s", src.name, e)
            if src.pause_ttl is not None:
                self._pool.pause_source(src.name, ttl=src.pause_ttl)
                logger.info("代理源 %s 已暂停 %d 小时", src.name, src.pause_ttl // 3600)

        # 第二阶段：跨源去重入组
        new_count = 0
        seen = set()
        for src_name, (proxy_list, src) in source_results.items():
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

        # 第三阶段：去重率过高则暂停源
        for src_name, (proxy_list, src) in source_results.items():
            if not proxy_list:
                continue
            known = sum(1 for p in proxy_list if self._pool.is_known_proxy(p))
            dedup_rate = known / len(proxy_list)
            if dedup_rate > self._dedup_threshold:
                self._pool.pause_source(src_name)
                logger.info("代理源 %s 去重率 %.0f%%, 暂停 %d 分钟",
                            src_name, dedup_rate * 100, self._pool.default_pause_ttl // 60)


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
