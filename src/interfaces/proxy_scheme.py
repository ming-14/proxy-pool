"""出站代理协议的机制描述表（接口适配器层：协议知识的唯一归属）

池内代理是 "scheme://host:port" 字符串，scheme 被三处消费，此前各自硬编码判断前缀；
集中到本表后新增协议只改这里，调用方不再解析 scheme：
    engine_uri              pproxy 上游 URI 映射（https → http+ssl，TLS 到代理）
    needs_socks_connector   探测客户端选择（aiohttp 原生 proxy= 只认 http/https）
    verify_target           探测时是否校验目标证书（https 代理校验的是代理自签证书）

**策略 vs 机制**：池"支持哪些协议"是策略，定义在 domain.SUPPORTED_SCHEMES（入池校验）；
本表只回答"每种协议怎么用"，属机制——因此仍含策略已移除的 socks4（pproxy 与
aiohttp_socks 的 socks4 能力保留，供直接使用这些库的场景与既有回归用例）。
"""

from __future__ import annotations

from typing import NamedTuple

from src.domain.models import bind_of, proxy_type


class SchemeSpec(NamedTuple):
    """单个出站协议的机制参数

    engine:        pproxy 上游 URI 的 scheme（TLS 到代理用 pproxy 的 http+ssl 表示）
    socks_client:  探测是否需 aiohttp_socks.ProxyConnector
    verify_target: 探测时是否校验目标证书
    """

    engine: str
    socks_client: bool
    verify_target: bool


_SPECS: dict[str, SchemeSpec] = {
    "http": SchemeSpec("http", False, True),
    # https 代理的 TLS 只到代理本身，证书是代理自签的（免费代理必然失败）→ 不校验
    "https": SchemeSpec("http+ssl", False, False),
    "socks4": SchemeSpec("socks4", True, True),
    "socks5": SchemeSpec("socks5", True, True),
}


def engine_uri(proxy: str) -> str:
    """池内代理字符串 → pproxy 上游 URI

    scheme 与引擎 scheme 一致（或表外 scheme，如 pproxy 的 direct://）时**原样返回**：
    不重建字符串，保证 _uri_index 的匹配键与池内字符串的对应关系稳定。
    """
    scheme = proxy_type(proxy)
    spec = _SPECS.get(scheme)
    if spec is None or spec.engine == scheme:
        return proxy
    return f"{spec.engine}://{bind_of(proxy)}"


def needs_socks_connector(proxy: str | None) -> bool:
    """该代理探测是否需 socks 连接器（aiohttp 原生 proxy= 只支持 http/https）"""
    if not proxy:
        return False
    spec = _SPECS.get(proxy_type(proxy))
    return bool(spec and spec.socks_client)


def verify_target(proxy: str) -> bool:
    """该代理探测时是否校验目标证书

    http/socks 代理 + https 目标走隧道端到端 TLS，校验的是**目标**证书，可筛掉伪造证书的
    中间人代理 → True；https 代理的 TLS 直连代理本身，校验的是代理自签证书，免费代理会全灭
    → False。表外 scheme 取 True（保守：宁可探测失败，也不放过伪造证书的链路）。
    """
    spec = _SPECS.get(proxy_type(proxy))
    return spec.verify_target if spec else True