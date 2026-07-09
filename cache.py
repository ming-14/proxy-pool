"""代理缓存管理器

管理代理池的分组、TTL 过期淘汰、源暂停、持久化存储。

缓存结构:
    proxy_groups        代理分组，每组独立 TTL，代理同时只存在于一个组中
        default         默认组，新代理入组位置，TTL 较短
        trusted         信任池，成功代理晋级至此，永不过期（TTL=None）
        blacklist       黑名单，失败代理沉入至此，TTL 较长
        <源名>          各代理源独立组（如 freeproxy-world），TTL 由配置决定

    淘汰规则:
        - default 组:   fails > MAX_FAILS 时移至 blacklist
        - trusted 组:   fails > TRUSTED_MAX_FAILS 时移至 blacklist
        - 所有组:       joined_at + ttl < now 时自动过期移除（trusted 除外）

    source_paused       代理源暂停记录，某源连续失败后暂停该源一段时间
        paused_at       暂停起始时间（秒级时间戳）
        ttl             暂停持续秒数，过期后自动恢复

持久化: 内存状态序列化为 JSON 写入 .cache 文件，加载时自动过滤已过期条目。
"""
import json
import os
import time

from config import CACHE_FILE, PROXY_GROUPS, MAX_FAILS, TRUSTED_MAX_FAILS, SOURCE_PAUSE_TTL


class CacheManager:
    """代理缓存管理器，统一管理所有代理组

    所有代理存储在 proxy_groups 中，每组有独立的 TTL。
    每个代理记录 joined_at（入组时间），达到 TTL 自动退出。
    信任池 TTL 为 None（永不过期），黑名单 TTL 为 BLACKLIST_TTL。
    代理同时只存在于一个组中。
    """

    def __init__(self, cache_file=None):
        self._cache_file = cache_file or CACHE_FILE
        self.proxy_groups = {}
        self.source_paused = {}
        self._init_groups()
        self._load_cache()

    def _init_groups(self):
        for name, ttl in PROXY_GROUPS.items():
            if name not in self.proxy_groups:
                self.proxy_groups[name] = {"ttl": ttl, "proxies": {}}

    def _is_active(self, group_name, proxy):
        group = self.proxy_groups.get(group_name)
        if not group or proxy not in group["proxies"]:
            return False
        if group["ttl"] is None:
            return True
        return time.time() - group["proxies"][proxy]["joined_at"] < group["ttl"]

    def find_proxy(self, proxy):
        """查找代理所在的活跃组，过期代理自动清除

        Returns:
            (组名, 代理信息dict) 或 (None, None)
        """
        for name, group in self.proxy_groups.items():
            if proxy not in group["proxies"]:
                continue
            if self._is_active(name, proxy):
                return name, group["proxies"][proxy]
            del group["proxies"][proxy]
            return None, None
        return None, None

    def add_to_group(self, group_name, proxy, fails=0):
        if group_name not in self.proxy_groups:
            return
        self.proxy_groups[group_name]["proxies"][proxy] = {
            "joined_at": int(time.time()),
            "fails": fails,
        }

    def move_proxy(self, proxy, to_group, fails=None):
        """将代理从当前组移到目标组

        Args:
            proxy: 代理地址
            to_group: 目标组名
            fails: 失败次数，None则保留原值
        """
        src_group, info = self.find_proxy(proxy)
        if src_group is not None:
            del self.proxy_groups[src_group]["proxies"][proxy]
        actual_fails = fails if fails is not None else (info["fails"] if info else 0)
        self.add_to_group(to_group, proxy, fails=actual_fails)

    def is_blacklisted(self, proxy):
        return self._is_active("blacklist", proxy)

    def is_known_proxy(self, proxy):
        if self.is_blacklisted(proxy):
            return True
        return self._is_active("trusted", proxy)

    def get_available_proxies(self, exclude=None):
        """获取所有可请求组中的活跃代理（排除黑名单、超失败数和指定代理）

        可请求组: default, freeproxy-world, trusted（不含blacklist）

        Returns:
            dict: {proxy: {"group": 组名, "fails": 失败次数}}
        """
        exclude = exclude or set()
        self._cleanup_all_expired()
        result = {}
        for name in ["default", "freeproxy-world", "trusted"]:
            group = self.proxy_groups.get(name)
            if not group:
                continue
            max_fails = TRUSTED_MAX_FAILS if name == "trusted" else MAX_FAILS
            for proxy, info in group["proxies"].items():
                if proxy in exclude or proxy in result:
                    continue
                if self.is_blacklisted(proxy):
                    continue
                if info["fails"] >= max_fails:
                    continue
                result[proxy] = {"group": name, "fails": info["fails"]}
        return result

    def get_trusted_proxies(self):
        group = self.proxy_groups.get("trusted", {})
        return [
            p for p, info in group.get("proxies", {}).items()
            if info["fails"] < TRUSTED_MAX_FAILS and self._is_active("trusted", p)
        ]

    def get_fail_count(self, proxy):
        _, info = self.find_proxy(proxy)
        return info["fails"] if info else 0

    def _cleanup_all_expired(self):
        now = time.time()
        for name, group in self.proxy_groups.items():
            if group["ttl"] is None:
                continue
            expired = [
                p for p, info in group["proxies"].items()
                if now - info["joined_at"] >= group["ttl"]
            ]
            for p in expired:
                del group["proxies"][p]

    def pause_source(self, source, ttl=None):
        self.source_paused[source] = {"paused_at": int(time.time()), "ttl": ttl}

    def is_source_paused(self, source):
        if source not in self.source_paused:
            return False
        info = self.source_paused[source]
        ttl = info.get("ttl") or SOURCE_PAUSE_TTL
        if time.time() - info["paused_at"] >= ttl:
            del self.source_paused[source]
            return False
        return True

    def _load_cache(self):
        if not os.path.exists(self._cache_file):
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._init_groups()
            now = time.time()
            for name, group_data in data.get("proxy_groups", {}).items():
                if name not in self.proxy_groups:
                    continue
                self.proxy_groups[name]["ttl"] = group_data.get("ttl", self.proxy_groups[name]["ttl"])
                for p in group_data.get("proxies", []):
                    joined_at = p["joined_at"]
                    ttl = self.proxy_groups[name]["ttl"]
                    if ttl is not None and now - joined_at >= ttl:
                        continue
                    self.proxy_groups[name]["proxies"][p["proxy"]] = {
                        "joined_at": joined_at,
                        "fails": p.get("fails", 0),
                    }
            self.source_paused = {}
            for src, val in data.get("source_paused", {}).items():
                paused_at = val["paused_at"]
                ttl = val.get("ttl") or SOURCE_PAUSE_TTL
                if time.time() - paused_at < ttl:
                    self.source_paused[src] = {"paused_at": paused_at, "ttl": ttl}
        except Exception:
            self._init_groups()
            self.source_paused = {}

    def save(self):
        self._cleanup_all_expired()
        data = {
            "proxy_groups": {},
            "source_paused": dict(self.source_paused),
        }
        for name, group in self.proxy_groups.items():
            proxies = [
                {"proxy": proxy, "joined_at": info["joined_at"], "fails": info["fails"]}
                for proxy, info in group["proxies"].items()
            ]
            data["proxy_groups"][name] = {
                "ttl": group["ttl"],
                "proxies": proxies,
            }
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
