"""DoH 域名解析适配器（HostResolver 端口实现：穿指定代理做远端 DNS 查询）

目标地区探测需要**该代理出口视角**的解析结果（CDN 按出口就近调度，不同代理
解析落点不同），而 HTTP/SOCKS 隧道内无法回读远端解析出的 IP——唯一现实路径
是经该代理请求 DoH（DNS over HTTPS）JSON 接口：查询由代理出口发出，响应里的
A 记录即该出口的解析结果。

端点按配置顺序回退（免费代理不可靠，且国外端点可能在出口侧被阻断，配置里带
国内端点兜底）；单次 resolve 内不重试，失败退避由用例层统一控制。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from src.interfaces.proxy_scheme import verify_target

if TYPE_CHECKING:  # 仅类型标注引用，运行时不绑定具体实现（测试可注入假的 getter）
    from src.interfaces.http_client import AsyncHttpGetter

logger = logging.getLogger(__name__)

# 每个域名最多返回的 A 记录数：定位取首个可用地址即可，多地址只增定位开销
_MAX_ADDRESSES = 3

# DoH JSON 响应格式由 Accept 头声明（Cloudflare/Google/alidns 均支持）
_DOH_HEADERS = {"Accept": "application/dns-json"}


class DohResolver:
    """HostResolver 端口实现：按顺序尝试 DoH 端点，返回 A 记录

    非 200 / 非法 JSON / 网络异常 → 换下一个端点；全部失败抛最后一个异常。
    HTTP 200 且 JSON 合法但无 Answer（NXDOMAIN/SERVFAIL 等）视为**成功且无结果**
    （空列表），不换端点——负结果若触发回退会被端点放大成多次查询，"无结果"
    的落盘缓存由用例层负责。
    """

    def __init__(self, endpoints: list[str], http_getter: AsyncHttpGetter,
                 *, timeout: float = 8.0) -> None:
        self._endpoints = list(endpoints)
        self._getter = http_getter
        self._timeout = timeout

    async def resolve(self, host: str, *, proxy: str) -> list[str]:
        """查询 host 的 A 记录（经 proxy 出口；失败抛异常，空列表=有效但无结果）"""
        name = _idna(host)
        # 证书策略复用探测口径：http/socks 代理隧道端到端校验目标证书；https 代理
        # 的 TLS 只到代理本身（自签证书），校验会让免费代理全灭。
        verify = verify_target(proxy)
        last_exc: Exception | None = None
        for endpoint in self._endpoints:
            url = _query_url(endpoint, name)
            started = time.monotonic()
            try:
                payload = await self._getter.get_json(
                    url, timeout=self._timeout, headers=_DOH_HEADERS,
                    proxy=proxy, verify=verify,
                )
                addresses = _extract_a_records(payload)
            except Exception as exc:
                # 端点回退是本适配器的职责：单端点失败（超时/被阻断/坏响应）不应
                # 让整个探测失败，继续尝试下一个端点；退避由上层的 pair 级控制。
                last_exc = exc
                logger.debug("DoH 端点失败 host=%s endpoint=%s err=%s", host, endpoint, exc)
                continue
            logger.debug("DoH 解析成功 host=%s endpoint=%s addrs=%d 耗时=%.0fms",
                         host, endpoint, len(addresses), (time.monotonic() - started) * 1000)
            return addresses
        raise RuntimeError(
            f"DoH 全部端点失败 host={host!r} proxy={proxy}: {last_exc}"
        ) from last_exc


def _query_url(endpoint: str, name: str) -> str:
    """拼接 DoH JSON 查询 URL（端点可含路径，如 /dns-query、/resolve）"""
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}name={quote(name, safe='')}&type=A"


def _idna(host: str) -> str:
    """主机名 → ASCII 形式（IDN 转 punycode；非法主机名转 ValueError）

    标准库 idna 编解码：ASCII 域名原样返回，非 ASCII 走 nameprep+punycode
    （如 例子.测试 → xn--fsqu00a.xn--0zwm56d）；空标签/超长标签抛 UnicodeError,
    转 ValueError 让用例层按"跳过该 host"处理。
    """
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"主机名无法 IDNA 编码: {host!r}") from exc


def _extract_a_records(payload: Any) -> list[str]:
    """从 DoH JSON 响应提取 A 记录（type==1 的 data），去重、最多 _MAX_ADDRESSES 个

    CNAME（type==5）等其它记录忽略：递归解析由 DoH 端完成，最终 A 记录才是定位依据。
    """
    if not isinstance(payload, dict):
        raise ValueError("DoH 响应不是 JSON 对象")
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []
    out: list[str] = []
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != 1:
            continue
        data = answer.get("data")
        if not isinstance(data, str) or not data:
            continue
        if data not in out:
            out.append(data)
        if len(out) >= _MAX_ADDRESSES:
            break
    return out
