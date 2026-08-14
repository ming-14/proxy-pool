"""领域层值对象"""

from __future__ import annotations

from dataclasses import dataclass


def normalize_proxy(proxy: str) -> str:
    """代理地址规范化：裸 ip:port 补默认 http:// scheme（CLI 输入、旧缓存迁移共用）"""
    return proxy if "://" in proxy else f"http://{proxy}"


def bind_of(proxy: str) -> str:
    """提取代理地址部分（不含 scheme 前缀），与 pproxy.Connection.bind 一致

    "http://1.2.3.4:8080" → "1.2.3.4:8080"，"DIRECT" → "DIRECT"。
    per-proxy 流量统计的 key 是 roption.bind（不含 scheme），延时历史用此函数
    对齐，使两种历史按同一 key（bind）索引。
    """
    return proxy.split("://", 1)[1] if "://" in proxy else proxy


def to_proxy_dict(proxy: str) -> dict[str, str]:
    """构造 HTTP 客户端代理字典 {http, https}

    https:// 前缀是真正的 HTTPS 代理（代理本身走 TLS），requests 支持该前缀；
    心跳/check_proxies 经 http_client 探测时，检测到 https:// 前缀自动退回 requests。
    """
    p = normalize_proxy(proxy)
    return {"http": p, "https": p}


@dataclass(frozen=True)
class SourceSpec:
    """代理源元数据（组合根从 config.SOURCES 构建）

    name:      源模块导入路径（同时是源暂停记录 key），如 "src.interfaces.sources.freeproxy_world"
    group:     拉取到的代理入组名，如 "default" / "freeproxy-world"
    enabled:   是否启用
    pause_ttl: 每次拉取后暂停该源的时长（秒），None 表示不主动暂停
    group_by_protocol: 按代理 scheme 前缀分拣入组（组名 = group + "-" + scheme），
               多协议源（freeproxy_world 产出 http/https/socks5）置 True
    """

    name: str
    group: str
    enabled: bool
    pause_ttl: int | None
    group_by_protocol: bool = False
