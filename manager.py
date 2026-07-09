import importlib
import logging
import random
import time
import requests

from cache import CacheManager
from config import MAX_FAILS, TRUSTED_MAX_FAILS, SOURCE_PAUSE_TTL, SOURCES, SOURCE_DEDUP_THRESHOLD, FETCH_PROXY_RETRIES
from log import setup_logging
setup_logging()

logger = logging.getLogger(__name__)


class ProxyManager:
    """代理池管理器，负责代理的获取、调度、成功/失败记录和淘汰

    代理按组管理：default（4分钟TTL）、freeproxy-world（5小时TTL）、trusted（永不过期）、blacklist（3小时TTL）
    """

    def __init__(self, cache_file=None):
        self._cache = CacheManager(cache_file=cache_file)

    def _fetch_proxies(self):
        """从所有启用的代理源拉取代理，按组写入，并检查源去重率决定是否暂停"""
        active_sources = [s for s in SOURCES if s["enabled"]]
        sources_to_call = []
        for src in active_sources:
            if self._cache.is_source_paused(src["name"]):
                info = self._cache.source_paused[src["name"]]
                ttl = info.get("ttl") or SOURCE_PAUSE_TTL
                remaining = int(ttl - (time.time() - info["paused_at"]))
                logger.info("代理源 %s 已暂停, 剩余 %ds", src["name"], max(remaining, 0))
                continue
            sources_to_call.append(src)
        logger.info("活跃代理源: %d/%d", len(sources_to_call), len(active_sources))
        source_results = {}
        for src in sources_to_call:
            try:
                mod = importlib.import_module(src["name"])
                proxy_list = mod.fetch_proxies(retries=FETCH_PROXY_RETRIES)
                logger.info("代理源 %s 返回 %d 个代理", src["name"], len(proxy_list))
                source_results[src["name"]] = (proxy_list, src["group"])
            except Exception as e:
                logger.warning("代理源 %s 失败: %s", src["name"], e)
            if src["pause_ttl"] is not None:
                self._cache.pause_source(src["name"], ttl=src["pause_ttl"])
                logger.info("代理源 %s 已暂停 %d 小时", src["name"], src["pause_ttl"] // 3600)
        new_count = 0
        seen = set()
        for src_name, (proxy_list, group_name) in source_results.items():
            for p in proxy_list:
                if p in seen or self._cache.find_proxy(p)[0] is not None or self._cache.is_blacklisted(p):
                    continue
                seen.add(p)
                self._cache.add_to_group(group_name, p)
                new_count += 1
        available = self._cache.get_available_proxies()
        trusted_group = self._cache.proxy_groups.get("trusted", {}).get("proxies", {})
        blacklist_group = self._cache.proxy_groups.get("blacklist", {}).get("proxies", {})
        logger.info("去重后新增 %d 个代理 (可请求 %d, 信任池 %d, 黑名单 %d)",
                      new_count, len(available), len(trusted_group), len(blacklist_group))
        for src_name, (proxy_list, group_name) in source_results.items():
            if not proxy_list:
                continue
            known = sum(1 for p in proxy_list if self._cache.is_known_proxy(p))
            dedup_rate = known / len(proxy_list)
            if dedup_rate > SOURCE_DEDUP_THRESHOLD:
                self._cache.pause_source(src_name)
                logger.info("代理源 %s 去重率 %.0f%%, 暂停 %d 分钟", src_name, dedup_rate * 100, SOURCE_PAUSE_TTL // 60)

    def save_cache(self):
        self._cache.save()

    def get_proxies(self, n, exclude=None):
        """获取最多n个可用代理，可排除已尝试的代理；池空时自动拉取"""
        available = self._cache.get_available_proxies(exclude)
        if not available:
            self._fetch_proxies()
            available = self._cache.get_available_proxies(exclude)
        if not available:
            logger.warning("代理池耗尽, 已排除 %d", len(exclude or set()))
            return []
        count = min(n, len(available))
        return random.sample(list(available.keys()), count)

    def record_success(self, proxy):
        """记录代理成功，将其从当前组移入信任池"""
        group_name, _ = self._cache.find_proxy(proxy)
        if group_name and group_name != "trusted":
            self._cache.move_proxy(proxy, "trusted", fails=0)
            logger.info("代理 %s 已加入信任列表", proxy)

    def record_fail(self, proxy):
        """记录代理失败，累加失败计数"""
        group_name, info = self._cache.find_proxy(proxy)
        if info is not None:
            info["fails"] += 1
        logger.info("代理 %s 失败计数: %d", proxy, self.get_fail_count(proxy))

    def remove(self, proxy):
        """移除代理并加入黑名单"""
        self._cache.move_proxy(proxy, "blacklist", fails=0)

    def get_fail_count(self, proxy):
        return self._cache.get_fail_count(proxy)

    def get_trusted_proxies(self):
        return self._cache.get_trusted_proxies()


if __name__ == "__main__":
    import sys
    import json

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    pm = ProxyManager()
    try:
        pm._fetch_proxies()
        available = pm._cache.get_available_proxies()
        print(json.dumps(list(available.keys()), ensure_ascii=False))
    finally:
        pm.save_cache()
