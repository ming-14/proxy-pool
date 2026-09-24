"""IP 纯净度检查插件公共骨架：超时/代理/证书策略 + JSON 请求

requests 方法级导入：插件模块只在纯净度启用的分支被 import，本模块不引入顶层
第三方依赖，serve（面板关闭或源禁用）不加载 requests 链。
"""

from __future__ import annotations


class HttpPurityChecker:
    """HTTP 类检查源公共实现

    代理非空时经代理访问并禁用证书校验（代理证书常与目标 IP 不匹配，校验会误杀）。
    """

    def __init__(self, *, timeout: float, proxy: str) -> None:
        self._timeout = timeout
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._verify = not bool(proxy)
        if proxy:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get_json(self, url: str, *, params: dict, headers: dict | None = None) -> dict:
        """GET 并解析 JSON；非 2xx 抛 requests.HTTPError（由 Web 层转 500 展示）"""
        import requests
        resp = requests.get(url, params=params, headers=headers, timeout=self._timeout,
                            proxies=self._proxies, verify=self._verify)
        resp.raise_for_status()
        return resp.json()

    def check(self, ip: str) -> dict:
        """查询单个 IP：返回 {score, usage_type, extra}，由子类实现"""
        raise NotImplementedError


def compact(values: dict) -> dict:
    """丢弃 None 值：源未返回的字段不进 extra，不塞占位"""
    return {key: value for key, value in values.items() if value is not None}
