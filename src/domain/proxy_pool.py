"""代理池领域模型：分组 / TTL / 晋级 / 淘汰 / 过期清理 / 源暂停 / 状态序列化

纯内存、无 IO、无第三方依赖。持久化由 export_state()/restore_state() 与
interfaces.SqliteProxyStore 协作：store.load() 的结果喂给 restore_state()，
export_state() 的输出交给 store.save_full()，export_group() 交给 store.save_group()。

数据形状:
    proxy_groups:   {group_name: {"ttl": int|None, "proxies": {proxy: {"joined_at": int, "consecutive_fails": int}}}}
    source_paused:  {source_name: {"paused_at": int, "ttl": int|None}}

consecutive_fails 为连续失败计数：每次失败 +1，任何成功（请求成功/心跳成功/
补测成功）清零；淘汰以连续失败计数为准，瞬时失败不会永久累积。

淘汰规则:
    - 非信任池组: consecutive_fails >= max_consecutive_fails 的代理不参与可用集合
    - 信任池组:   consecutive_fails >= trusted_max_consecutive_fails 不参与
    - 所有组:        joined_at + ttl < now 自动过期移除（ttl=None 永不过期）

特殊组名（信任池/黑名单）由配置指定，经构造参数注入，本模块不硬编码。
LOCKED_GROUP 为内建隐藏组（不在配置文件，运行时承载锁定代理），
serve lock 时代理 move 到该组作为 pproxy 唯一上游。
"""

from __future__ import annotations

import time
from typing import Any

from src.domain.models import normalize_proxy

# 内建锁定组（不在配置文件，ProxyPool.__init__ 自动创建，ttl=never 永不过期）。
# serve lock 时代理 move 到该组，standby_proxies 只返回该组代理作为 pproxy 唯一上游；
# unlock 时代理 move 回代理服务队列。对前端/组列表隐藏。
LOCKED_GROUP = "_proxy-pool-locked"


class ProxyPool:
    """代理池领域模型，统一管理所有代理组与源暂停状态"""

    def __init__(
        self,
        groups: dict[str, int | None],
        *,
        max_consecutive_fails: int,
        trusted_max_consecutive_fails: int,
        default_pause_ttl: int,
        requestable_groups: tuple[str, ...],
        trusted_group: str,
        blacklist_group: str,
    ) -> None:
        """
        Args:
            groups:             分组名 -> TTL 秒数（None 表示永不过期），如 {"default-http": 3600, "trusted": None}
            max_consecutive_fails:          普通组代理的最大连续失败次数（超过后不再可用）
            trusted_max_consecutive_fails:  信任池组代理的最大连续失败次数
            default_pause_ttl:  源暂停的默认时长（秒），暂停时未显式指定 ttl 时使用
            requestable_groups: 可请求组名元组（get_available_proxies 遍历），如 ("default-http", "trusted")
            trusted_group:      信任池组名（成功代理晋级目标，由配置指定）
            blacklist_group:    黑名单组名（失败淘汰归属，由配置指定）
        """
        self.max_consecutive_fails = max_consecutive_fails
        self.trusted_max_consecutive_fails = trusted_max_consecutive_fails
        self.default_pause_ttl = default_pause_ttl
        self.requestable_groups = list(requestable_groups)
        self.trusted_group = trusted_group
        self.blacklist_group = blacklist_group
        self.proxy_groups: dict[str, dict] = {}
        self.source_paused: dict[str, dict] = {}
        self._last_cleanup: float = 0.0  # 过期清理节流：60s 内不重复清理
        # dirty tracking：标记自上次 save 后变更的组，Container.save 只持久化 dirty 组
        self._dirty_groups: set[str] = set()
        self._source_paused_dirty: bool = False
        self._init_groups(groups)
        # 内建锁定组（不在配置文件，运行时承载锁定代理，永不过期）
        if LOCKED_GROUP not in self.proxy_groups:
            self.proxy_groups[LOCKED_GROUP] = {"ttl": None, "proxies": {}}

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

    def add_to_group(self, group_name: str, proxy: str, consecutive_fails: int = 0,
                     latency: float | None = None, *,
                     pinned: bool = False, pinned_at: int | None = None,
                     source_group: str | None = None) -> None:
        """将代理加入指定组（覆盖旧记录）

        Args:
            latency:       探测响应时间（秒），standby 队列按此排序；None 表示未探测
            pinned:        是否固定（固定代理在代理服务队列排最前且不被踢出/拉黑）
            pinned_at:     固定时间戳（pinned=True 时有效，用于固定顺序排序）
            source_group:  来源组名（仅黑名单代理记录，标记从哪个组被拉黑，供前端展示）
        """
        if group_name not in self.proxy_groups:
            return
        proxy = normalize_proxy(proxy)
        self.proxy_groups[group_name]["proxies"][proxy] = {
            "joined_at": int(time.time()),
            "consecutive_fails": consecutive_fails,
            "latency": latency,
            "pinned": pinned,
            "pinned_at": pinned_at,
            "source_group": source_group,
        }
        self._dirty_groups.add(group_name)

    def remove_from_group(self, group_name: str, proxy: str) -> bool:
        """从指定组移除代理，返回是否实际移除"""
        proxy = normalize_proxy(proxy)
        group = self.proxy_groups.get(group_name)
        if group is None or proxy not in group["proxies"]:
            return False
        del group["proxies"][proxy]
        self._dirty_groups.add(group_name)
        return True

    def move_proxy(self, proxy: str, to_group: str, consecutive_fails: int | None = None,
                   latency: float | None = None) -> None:
        """将代理从当前组移到目标组（保留 pinned/pinned_at 固定状态）

        Args:
            proxy:    代理地址
            to_group: 目标组名
            consecutive_fails:    连续失败次数，None 则保留原值
            latency:  响应时间，None 则保留原值
        """
        proxy = normalize_proxy(proxy)
        src_group, info = self.find_proxy(proxy)
        if src_group is not None:
            del self.proxy_groups[src_group]["proxies"][proxy]
            self._dirty_groups.add(src_group)
        actual_consecutive_fails = consecutive_fails if consecutive_fails is not None else (info["consecutive_fails"] if info else 0)
        actual_latency = latency if latency is not None else (info.get("latency") if info else None)
        actual_pinned = info.get("pinned", False) if info else False
        actual_pinned_at = info.get("pinned_at") if info else None
        self.add_to_group(to_group, proxy, consecutive_fails=actual_consecutive_fails,
                          latency=actual_latency, pinned=actual_pinned, pinned_at=actual_pinned_at)

    def pin(self, proxy: str) -> None:
        """固定代理：标记 pinned + 计时。固定代理在代理服务队列排最前且不被踢出/拉黑"""
        group, info = self.find_proxy(proxy)
        if info is not None:
            info["pinned"] = True
            info["pinned_at"] = int(time.time())
            self._dirty_groups.add(group)

    def unpin(self, proxy: str) -> None:
        """解除固定"""
        group, info = self.find_proxy(proxy)
        if info is not None:
            info["pinned"] = False
            info.pop("pinned_at", None)
            self._dirty_groups.add(group)

    def is_pinned(self, proxy: str) -> bool:
        """代理是否被固定"""
        _, info = self.find_proxy(proxy)
        return info.get("pinned", False) if info else False

    def get_pinned_at(self, proxy: str) -> int | None:
        """获取固定时间戳（未固定返回 None）"""
        _, info = self.find_proxy(proxy)
        return info.get("pinned_at") if info else None

    def update_latency(self, proxy: str, latency: float) -> None:
        """更新代理响应时间（heartbeat 探测成功后更新，不改 joined_at/consecutive_fails/组归属）"""
        group, info = self.find_proxy(proxy)
        if info is not None:
            info["latency"] = latency
            self._dirty_groups.add(group)

    def get_latency(self, proxy: str) -> float | None:
        """获取代理响应时间（不存在或未探测返回 None）"""
        _, info = self.find_proxy(proxy)
        return info.get("latency") if info else None

    def remove_all_copies(self, proxy: str, to_group: str) -> None:
        """将代理从所有组移除（清理多组副本）后加入目标组，记录来源组

        复制进 trusted 的代理服务队列代理被拉黑时，残留副本必须在黑名单过期前
        清除，否则会复活为可请求代理。单次遍历所有组 pop（与 find_proxy
        同量级），不做反向索引，避免 O(组数×组内代理) 的二次扫描。

        来源组（source_group）：拉黑时代理所在的活跃组（find_proxy 定位），
        记入黑名单 info 供前端展示"从哪个组被拉黑"。find_proxy 开销 O(组数)，
        remove 本身已遍历所有组，额外开销可忽略；拉黑是低频操作不影响热路径。
        """
        proxy = normalize_proxy(proxy)
        src_group, _ = self.find_proxy(proxy)
        for name, group in self.proxy_groups.items():
            if group["proxies"].pop(proxy, None) is not None:
                self._dirty_groups.add(name)
        self.add_to_group(to_group, proxy, consecutive_fails=0, source_group=src_group)

    def is_blacklisted(self, proxy: str) -> bool:
        """代理是否在黑名单组且未过期"""
        return self._is_active(self.blacklist_group, normalize_proxy(proxy))

    def is_known_proxy(self, proxy: str) -> bool:
        """代理是否已存在于黑名单或信任池（用于去重率统计）"""
        if self.is_blacklisted(proxy):
            return True
        return self._is_active(self.trusted_group, normalize_proxy(proxy))

    def get_available_proxies(self, exclude: set[str] | None = None) -> dict[str, dict]:
        """获取所有可请求组中的活跃代理（排除黑名单、超连续失败数和指定代理）

        Returns:
            dict: {proxy: {"group": 组名, "consecutive_fails": 连续失败次数}}
        """
        exclude = {normalize_proxy(p) for p in (exclude or set())}
        self._cleanup_all_expired()
        # 预计算黑名单活跃集合（避免内循环每代理调 is_blacklisted → _is_active）
        bl_group = self.proxy_groups.get(self.blacklist_group, {})
        bl_ttl = bl_group.get("ttl")
        bl_proxies = bl_group.get("proxies", {})
        if bl_ttl is None:
            blacklisted = set(bl_proxies)
        else:
            now_bl = time.time()
            blacklisted = {p for p, info in bl_proxies.items()
                           if now_bl - info["joined_at"] < bl_ttl}
        result: dict[str, dict] = {}
        for name in self.requestable_groups:
            group = self.proxy_groups.get(name)
            if not group:
                continue
            max_consecutive_fails = self.trusted_max_consecutive_fails if name == self.trusted_group else self.max_consecutive_fails
            for proxy, info in group["proxies"].items():
                if proxy in exclude or proxy in result:
                    continue
                if proxy in blacklisted:
                    continue
                if info["consecutive_fails"] >= max_consecutive_fails:
                    continue
                result[proxy] = {"group": name, "consecutive_fails": info["consecutive_fails"]}
        return result

    def get_trusted_proxies(self) -> list[str]:
        """返回信任池中未超连续失败数的活跃代理"""
        group = self.proxy_groups.get(self.trusted_group, {})
        return [
            p
            for p, info in group.get("proxies", {}).items()
            if info["consecutive_fails"] < self.trusted_max_consecutive_fails and self._is_active(self.trusted_group, p)
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

    def reset_consecutive_fails(self, proxy: str, *, group: str | None = None) -> None:
        """清零代理连续失败计数（任何成功——请求/心跳/补测——均调用，体现连续语义）

        group 指定时仅在该组清零（用于 standby 成员请求成功时清零队列副本，
        不影响 trusted 副本）；None 时按 find_proxy 定位（单组场景）。
        """
        proxy = normalize_proxy(proxy)
        if group is not None:
            g = self.proxy_groups.get(group)
            if g and proxy in g["proxies"]:
                g["proxies"][proxy]["consecutive_fails"] = 0
                self._dirty_groups.add(group)
            return
        grp, info = self.find_proxy(proxy)
        if info is not None:
            info["consecutive_fails"] = 0
            self._dirty_groups.add(grp)

    def get_consecutive_fail_count(self, proxy: str) -> int:
        """获取代理当前连续失败次数（不存在返回 0）"""
        _, info = self.find_proxy(proxy)
        return info["consecutive_fails"] if info else 0

    def should_blacklist(self, proxy: str) -> bool:
        """代理连续失败次数是否达到所在组的拉黑阈值（trusted 容错更高）

        与 get_available_proxies 的淘汰规则同一标准：trusted 组用 trusted_max_consecutive_fails，
        其余组用 max_consecutive_fails；不存在或未过期则返回 False。
        """
        group_name, info = self.find_proxy(proxy)
        if info is None:
            return False
        threshold = self.trusted_max_consecutive_fails if group_name == self.trusted_group else self.max_consecutive_fails
        return info["consecutive_fails"] >= threshold

    def _cleanup_all_expired(self) -> None:
        """移除所有已过 TTL 的代理（ttl=None 的组跳过）

        节流：60 秒内不重复清理（代理最小 TTL=3600s，不会在 60s 内过期），
        get_available_proxies 高频调用时不重复遍历全池。
        """
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
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
            if expired:
                self._dirty_groups.add(name)

    def pause_source(self, source: str, ttl: int | None = None) -> None:
        """暂停代理源；ttl 缺省时用 default_pause_ttl（在 source_pause_remaining 中解析）"""
        self.source_paused[source] = {"paused_at": int(time.time()), "ttl": ttl}
        self._source_paused_dirty = True

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

    def dirty_groups(self) -> set[str]:
        """自上次 clear_dirty 后变更的组集合（供 Container.save 增量持久化）"""
        return set(self._dirty_groups)

    def source_paused_dirty(self) -> bool:
        """源暂停状态是否变更（供 Container.save 增量持久化）"""
        return self._source_paused_dirty

    def clear_dirty(self) -> None:
        """清空 dirty 标记（save 完成后调用）"""
        self._dirty_groups.clear()
        self._source_paused_dirty = False

    def export_group(self, group_name: str) -> dict:
        """导出单个组 {ttl, proxies: [...]}（供 save_group 用，不修改内存）

        过期代理不导出（ttl=None 全部导出），但不从内存删除（内存清理由
        _cleanup_all_expired 节流负责）；与 export_state 同格式但单组。
        """
        group = self.proxy_groups.get(group_name)
        if group is None:
            return {"ttl": None, "proxies": []}
        ttl = group["ttl"]
        now = time.time()
        proxies = []
        for proxy, info in group["proxies"].items():
            if ttl is not None and now - info["joined_at"] >= ttl:
                continue
            entry = {"proxy": proxy, "joined_at": info["joined_at"], "consecutive_fails": info["consecutive_fails"]}
            if info.get("latency") is not None:
                entry["latency"] = info["latency"]
            if info.get("pinned"):
                entry["pinned"] = True
                entry["pinned_at"] = info["pinned_at"]
            if info.get("source_group") is not None:
                entry["source_group"] = info["source_group"]
            proxies.append(entry)
        return {"ttl": ttl, "proxies": proxies}

    def export_source_paused(self) -> dict:
        """导出源暂停状态（与 export_state 同格式，浅拷贝）"""
        return dict(self.source_paused)

    def export_state(self) -> dict:
        """导出可持久化状态（先清理过期条目）

        latency 仅在非 None 时导出。
        """
        self._cleanup_all_expired()
        data: dict[str, Any] = {"proxy_groups": {}, "source_paused": dict(self.source_paused)}
        for name, group in self.proxy_groups.items():
            proxies = []
            for proxy, info in group["proxies"].items():
                entry = {"proxy": proxy, "joined_at": info["joined_at"], "consecutive_fails": info["consecutive_fails"]}
                if info.get("latency") is not None:
                    entry["latency"] = info["latency"]
                if info.get("pinned"):
                    entry["pinned"] = True
                    entry["pinned_at"] = info["pinned_at"]
                if info.get("source_group") is not None:
                    entry["source_group"] = info["source_group"]
                proxies.append(entry)
            data["proxy_groups"][name] = {"ttl": group["ttl"], "proxies": proxies}
        return data

    def restore_state(self, data: dict) -> None:
        """从 store.load() 结果恢复状态：组 ttl 以配置为准、过期条目过滤、源暂停过期过滤

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
                    "consecutive_fails": p.get("consecutive_fails", 0),
                    "latency": p.get("latency"),
                    "pinned": p.get("pinned", False),
                    "pinned_at": p.get("pinned_at"),
                    "source_group": p.get("source_group"),
                }
        self.source_paused = {}
        for src, val in data.get("source_paused", {}).items():
            paused_at = val["paused_at"]
            ttl = val.get("ttl") or self.default_pause_ttl
            if time.time() - paused_at < ttl:
                self.source_paused[src] = {"paused_at": paused_at, "ttl": ttl}
