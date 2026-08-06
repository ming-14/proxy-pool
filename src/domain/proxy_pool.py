"""代理池领域模型：分组 / TTL / 晋级 / 淘汰 / 过期清理 / 源暂停 / 状态序列化

纯内存、无 IO、无第三方依赖。持久化由 export_state()/restore_state() 与
interfaces.JsonProxyStore 协作：store.load() 的结果喂给 restore_state()，
export_state() 的输出交给 store.save()。

数据形状（与旧 cache.py 一致，测试可原样迁移）:
    proxy_groups:   {group_name: {"ttl": int|None, "proxies": {proxy: {"joined_at": int, "fails": int}}}}
    source_paused:  {source_name: {"paused_at": int, "ttl": int|None}}

淘汰规则:
    - 非信任池组: fails >= max_fails 的代理不参与可用集合
    - 信任池组:   fails >= trusted_max_fails 不参与
    - 所有组:        joined_at + ttl < now 自动过期移除（ttl=None 永不过期）

特殊组名（信任池/黑名单）由配置指定，经构造参数注入，本模块不硬编码。
"""

import time
from typing import Any

from src.domain.models import normalize_proxy


class ProxyPool:
    """代理池领域模型，统一管理所有代理组与源暂停状态"""

    def __init__(
        self,
        groups: dict[str, int | None],
        *,
        max_fails: int,
        trusted_max_fails: int,
        default_pause_ttl: int,
        requestable_groups: tuple[str, ...],
        trusted_group: str,
        blacklist_group: str,
    ) -> None:
        """
        Args:
            groups:             分组名 -> TTL 秒数（None 表示永不过期），如 {"default-http": 3600, "trusted": None}
            max_fails:          普通组代理的最大失败次数（超过后不再可用）
            trusted_max_fails:  信任池组代理的最大失败次数
            default_pause_ttl:  源暂停的默认时长（秒），暂停时未显式指定 ttl 时使用
            requestable_groups: 可请求组名元组（get_available_proxies 遍历），如 ("default-http", "trusted")
            trusted_group:      信任池组名（成功代理晋级目标，由配置指定）
            blacklist_group:    黑名单组名（失败淘汰归属，由配置指定）
        """
        self.max_fails = max_fails
        self.trusted_max_fails = trusted_max_fails
        self.default_pause_ttl = default_pause_ttl
        self.requestable_groups = list(requestable_groups)
        self.trusted_group = trusted_group
        self.blacklist_group = blacklist_group
        self.proxy_groups: dict[str, dict] = {}
        self.source_paused: dict[str, dict] = {}
        self._init_groups(groups)

    def _init_groups(self, groups: dict[str, int | None]) -> None:
        """按配置初始化分组骨架（不覆盖已有数据）"""
        for name, ttl in groups.items():
            if name not in self.proxy_groups:
                self.proxy_groups[name] = {"ttl": ttl, "proxies": {}}

    def _is_active(self, group_name: str, proxy: str) -> bool:
        """判断代理在组内且未过期（ttl=None 永不过期），过期由调用方清理"""
        group = self.proxy_groups.get(group_name)
        if not group or proxy not in group["proxies"]:
            return False
        if group["ttl"] is None:
            return True
        return time.time() - group["proxies"][proxy]["joined_at"] < group["ttl"]

    def find_proxy(self, proxy: str) -> tuple[str | None, dict | None]:
        """查找代理所在的活跃组，过期代理自动清除

        Returns:
            (组名, 代理信息dict) 或 (None, None)
        """
        proxy = normalize_proxy(proxy)
        for name, group in self.proxy_groups.items():
            if proxy not in group["proxies"]:
                continue
            if self._is_active(name, proxy):
                return name, group["proxies"][proxy]
            del group["proxies"][proxy]
            return None, None
        return None, None

    def add_to_group(self, group_name: str, proxy: str, fails: int = 0,
                     latency: float | None = None) -> None:
        """将代理加入指定组（覆盖旧记录）

        Args:
            latency: 探测响应时间（秒），standby 队列按此排序；None 表示未探测
        """
        if group_name not in self.proxy_groups:
            return
        proxy = normalize_proxy(proxy)
        self.proxy_groups[group_name]["proxies"][proxy] = {
            "joined_at": int(time.time()),
            "fails": fails,
            "latency": latency,
        }

    def move_proxy(self, proxy: str, to_group: str, fails: int | None = None,
                   latency: float | None = None) -> None:
        """将代理从当前组移到目标组

        Args:
            proxy:    代理地址
            to_group: 目标组名
            fails:    失败次数，None 则保留原值
            latency:  响应时间，None 则保留原值
        """
        proxy = normalize_proxy(proxy)
        src_group, info = self.find_proxy(proxy)
        if src_group is not None:
            del self.proxy_groups[src_group]["proxies"][proxy]
        actual_fails = fails if fails is not None else (info["fails"] if info else 0)
        actual_latency = latency if latency is not None else (info.get("latency") if info else None)
        self.add_to_group(to_group, proxy, fails=actual_fails, latency=actual_latency)

    def update_latency(self, proxy: str, latency: float) -> None:
        """更新代理响应时间（heartbeat 探测成功后更新，不改 joined_at/fails/组归属）"""
        _, info = self.find_proxy(proxy)
        if info is not None:
            info["latency"] = latency

    def get_latency(self, proxy: str) -> float | None:
        """获取代理响应时间（不存在或未探测返回 None）"""
        _, info = self.find_proxy(proxy)
        return info.get("latency") if info else None

    def remove_all_copies(self, proxy: str, to_group: str) -> None:
        """将代理从所有组移除（清理多组副本）后加入目标组

        复制进 trusted 的备用队列代理被拉黑时，残留副本必须在黑名单过期前
        清除，否则会复活为可请求代理。单次遍历所有组 pop（与 find_proxy
        同量级），不做反向索引，避免 O(组数×组内代理) 的二次扫描。
        """
        proxy = normalize_proxy(proxy)
        for group in self.proxy_groups.values():
            group["proxies"].pop(proxy, None)
        self.add_to_group(to_group, proxy, fails=0)

    def is_blacklisted(self, proxy: str) -> bool:
        """代理是否在黑名单组且未过期"""
        return self._is_active(self.blacklist_group, normalize_proxy(proxy))

    def is_known_proxy(self, proxy: str) -> bool:
        """代理是否已存在于黑名单或信任池（用于去重率统计）"""
        if self.is_blacklisted(proxy):
            return True
        return self._is_active(self.trusted_group, normalize_proxy(proxy))

    def get_available_proxies(self, exclude: set[str] | None = None) -> dict[str, dict]:
        """获取所有可请求组中的活跃代理（排除黑名单、超失败数和指定代理）

        Returns:
            dict: {proxy: {"group": 组名, "fails": 失败次数}}
        """
        exclude = {normalize_proxy(p) for p in (exclude or set())}
        self._cleanup_all_expired()
        result: dict[str, dict] = {}
        for name in self.requestable_groups:
            group = self.proxy_groups.get(name)
            if not group:
                continue
            max_fails = self.trusted_max_fails if name == self.trusted_group else self.max_fails
            for proxy, info in group["proxies"].items():
                if proxy in exclude or proxy in result:
                    continue
                if self.is_blacklisted(proxy):
                    continue
                if info["fails"] >= max_fails:
                    continue
                result[proxy] = {"group": name, "fails": info["fails"]}
        return result

    def get_trusted_proxies(self) -> list[str]:
        """返回信任池中未超失败数的活跃代理"""
        group = self.proxy_groups.get(self.trusted_group, {})
        return [
            p
            for p, info in group.get("proxies", {}).items()
            if info["fails"] < self.trusted_max_fails and self._is_active(self.trusted_group, p)
        ]

    def get_group_proxies(self, group: str) -> list[str]:
        """组内全部代理地址（含过期条目，不做清理；组不存在返回空列表）"""
        return list(self.proxy_groups.get(group, {}).get("proxies", {}))

    def get_active_proxies(self, group: str) -> list[str]:
        """组内未过期的代理地址（ttl=None 的组全部活跃）"""
        return [p for p in self.get_group_proxies(group) if self._is_active(group, p)]

    def get_group_size(self, group: str) -> int:
        """组内代理数量（含过期条目；组不存在为 0）"""
        return len(self.get_group_proxies(group))

    def reset_fails(self, proxy: str) -> None:
        """清零代理失败计数（心跳/补测成功恢复时使用）"""
        _, info = self.find_proxy(proxy)
        if info is not None:
            info["fails"] = 0

    def get_fail_count(self, proxy: str) -> int:
        """获取代理当前失败次数（不存在返回 0）"""
        _, info = self.find_proxy(proxy)
        return info["fails"] if info else 0

    def should_blacklist(self, proxy: str) -> bool:
        """代理失败次数是否达到所在组的拉黑阈值（trusted 容错更高）

        与 get_available_proxies 的淘汰规则同一标准：trusted 组用 trusted_max_fails，
        其余组用 max_fails；不存在或未过期则返回 False。
        """
        group_name, info = self.find_proxy(proxy)
        if info is None:
            return False
        threshold = self.trusted_max_fails if group_name == self.trusted_group else self.max_fails
        return info["fails"] >= threshold

    def _cleanup_all_expired(self) -> None:
        """移除所有已过 TTL 的代理（ttl=None 的组跳过）"""
        now = time.time()
        for name, group in self.proxy_groups.items():
            if group["ttl"] is None:
                continue
            expired = [
                p
                for p, info in group["proxies"].items()
                if now - info["joined_at"] >= group["ttl"]
            ]
            for p in expired:
                del group["proxies"][p]

    def pause_source(self, source: str, ttl: int | None = None) -> None:
        """暂停代理源；ttl 缺省时用 default_pause_ttl（在 source_pause_remaining 中解析）"""
        self.source_paused[source] = {"paused_at": int(time.time()), "ttl": ttl}

    def source_pause_remaining(self, source: str) -> int | None:
        """源暂停剩余秒数；未暂停或已过期（自动解除并移除记录）返回 None

        统一各调用点（fetch 跳过提示 / cli status 显示 / 暂停判定）的
        ttl 解析与剩余时间计算，避免三处重复。
        """
        if source not in self.source_paused:
            return None
        info = self.source_paused[source]
        ttl = info.get("ttl") or self.default_pause_ttl
        remaining = ttl - (time.time() - info["paused_at"])
        if remaining <= 0:
            del self.source_paused[source]
            return None
        return int(remaining)

    def is_source_paused(self, source: str) -> bool:
        """源是否处于暂停期（复用 source_pause_remaining 的过期判定）"""
        return self.source_pause_remaining(source) is not None

    def export_state(self) -> dict:
        """导出可持久化状态（先清理过期条目），与旧 .cache 文件格式一致

        latency 仅在非 None 时导出（兼容旧读取方）。
        """
        self._cleanup_all_expired()
        data: dict[str, Any] = {"proxy_groups": {}, "source_paused": dict(self.source_paused)}
        for name, group in self.proxy_groups.items():
            proxies = []
            for proxy, info in group["proxies"].items():
                entry = {"proxy": proxy, "joined_at": info["joined_at"], "fails": info["fails"]}
                if info.get("latency") is not None:
                    entry["latency"] = info["latency"]
                proxies.append(entry)
            data["proxy_groups"][name] = {"ttl": group["ttl"], "proxies": proxies}
        return data

    def restore_state(self, data: dict) -> None:
        """从 store.load() 结果恢复状态：组 ttl 以配置为准、过期条目过滤、源暂停过期过滤

        兼容旧 .cache 文件格式（proxy_groups 为 dict，组内 proxies 为 list，无 latency 字段）。
        文件里保存的 ttl 不覆盖配置（TTL 是运行时配置，改配置即生效）。
        """
        now = time.time()
        for name, group_data in data.get("proxy_groups", {}).items():
            if name not in self.proxy_groups:
                continue
            ttl = self.proxy_groups[name]["ttl"]  # 以配置为准
            for p in group_data.get("proxies", []):
                proxy = normalize_proxy(p["proxy"])
                joined_at = p["joined_at"]
                if ttl is not None and now - joined_at >= ttl:
                    continue
                self.proxy_groups[name]["proxies"][proxy] = {
                    "joined_at": joined_at,
                    "fails": p.get("fails", 0),
                    "latency": p.get("latency"),
                }
        self.source_paused = {}
        for src, val in data.get("source_paused", {}).items():
            paused_at = val["paused_at"]
            ttl = val.get("ttl") or self.default_pause_ttl
            if time.time() - paused_at < ttl:
                self.source_paused[src] = {"paused_at": paused_at, "ttl": ttl}
