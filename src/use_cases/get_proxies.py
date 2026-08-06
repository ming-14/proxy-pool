"""获取代理用例（原 manager.get_proxies）

从池中随机取最多 n 个可用代理；池空时自动触发拉取。
"""

import logging
import random
from typing import Callable

from src.domain.models import normalize_proxy
from src.domain.proxy_pool import ProxyPool

logger = logging.getLogger(__name__)


class GetProxiesUseCase:
    """获取最多 n 个可用代理，可排除已尝试的代理；池空时自动拉取"""

    def __init__(
        self,
        *,
        pool: ProxyPool,
        fetch: Callable[[], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """
        Args:
            pool:   代理池领域模型
            fetch:  拉取回调（池空时调用），None 表示不自动拉取
            rng:    随机源，可注入以测试确定性
        """
        self._pool = pool
        self._fetch = fetch
        self._rng = rng or random

    def execute(self, n: int, exclude: set[str] | None = None) -> list[str]:
        """取最多 n 个可用代理

        优先 trusted 池（已通过心跳/请求验证的代理），不足再从其它可用组补，
        避免在大量死代理（如 freeproxy-world 的假代理）中随机命中失败。
        """
        exclude = {normalize_proxy(p) for p in (exclude or set())}

        def available_proxies() -> list[str]:
            available = self._pool.get_available_proxies(exclude)
            trusted = [p for p in self._pool.get_trusted_proxies()
                       if p in available and p not in exclude]
            others = [p for p in available if p not in trusted]
            return trusted, others

        trusted, others = available_proxies()
        if not trusted and not others:
            if self._fetch is not None:
                self._fetch()
                trusted, others = available_proxies()
        if not trusted and not others:
            logger.warning("代理池耗尽, 已排除 %d", len(exclude))
            return []

        # trusted 优先：先取 trusted，不足再用其它组补
        result = self._rng.sample(trusted, min(n, len(trusted))) if trusted else []
        if len(result) < n and others:
            result += self._rng.sample(others, min(n - len(result), len(others)))
        return result
