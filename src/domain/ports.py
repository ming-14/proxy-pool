"""领域层抽象端口（接口适配器层实现它们）

依赖方向：use_cases / interfaces 依赖这里的 Protocol，实现方无需继承，
只需满足结构签名。领域层本身零第三方依赖、无 IO。
"""

from typing import Any, Protocol


class ProxySource(Protocol):
    """代理源端口：拉取代理列表（ip:port 字符串）"""

    def fetch_proxies(
        self, retries: int | None = None, timeout: float | None = None
    ) -> list[str]: ...


class ProxyStore(Protocol):
    """代理池状态存储端口：纯读写，不含领域规则（过期过滤在 ProxyPool）"""

    def load(self) -> dict: ...
    def save(self, state: dict) -> None: ...


class HttpGetter(Protocol):
    """HTTP 请求端口：支持通过代理发起 GET；失败抛异常

    返回 duck-typed response（只读取 status_code/text/headers/encoding 属性），
    用例层不 import requests。
    verify: 是否校验证书。默认 False（免费代理多自签证书，直连校验会误杀）；
            探测类用例（check_proxies）对 http/socks5 代理显式传 True 以筛掉伪造证书的
            中间人代理（隧道端到端校验目标证书）；https 代理 TLS 直连代理本身，
            校验的是代理自签证书，仍传 False（见 check_proxies.do_test）。
    """

    def get(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict | None = None,
        proxies: dict | None = None,
        verify: bool = False,
    ) -> Any: ...


class TaskScheduler(Protocol):
    """并发任务调度器端口（CheckProxiesUseCase 只用到 run）"""

    async def run(
        self,
        tasks: list,
        phase_name: str,
        do_task: Any,
        on_success: Any = None,
    ) -> dict: ...
