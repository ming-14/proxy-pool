"""带重试的代理请求用例（原 scheduler.request_with_retry）

从池中取代理请求 URL，失败时记录并换代理重试，成功代理自动晋级信任池，
连续失败超阈值的代理自动拉黑。
"""

from __future__ import annotations

import logging

from src.domain.models import to_proxy_dict
from src.domain.ports import HttpGetter
from src.use_cases.get_proxies import GetProxiesUseCase
from src.use_cases.record_proxy import RecordProxyUseCase

logger = logging.getLogger(__name__)


class RequestWithRetryUseCase:
    """用代理池中的代理请求 URL，自动重试换代理

    返回 (proxy, response) 或抛出最后一个异常
    """

    def __init__(
        self,
        *,
        get_proxies: GetProxiesUseCase,
        record: RecordProxyUseCase,
        http_getter: HttpGetter,
    ) -> None:
        """
        Args:
            get_proxies:        取代理用例
            record:             记录用例（含按组拉黑判定）
            http_getter:        HTTP 请求端口实现
        """
        self._get_proxies = get_proxies
        self._record = record
        self._http_getter = http_getter

    def execute(
        self,
        url: str,
        max_retries: int = 5,
        timeout: float = 10,
        headers: dict | None = None,
    ) -> tuple[str, object]:
        tried: set[str] = set()
        last_exc = None

        for attempt in range(max_retries):
            proxy_list = self._get_proxies.execute(1, exclude=tried)
            if not proxy_list:
                break
            proxy = proxy_list[0]
            tried.add(proxy)
            proxy_dict = to_proxy_dict(proxy)
            try:
                resp = self._http_getter.get(url, timeout=timeout, headers=headers or {}, proxies=proxy_dict)
                self._record.record_success(proxy)
                return proxy, resp
            except Exception as e:
                self._record.record_fail(proxy)
                last_exc = e
                if self._record.should_blacklist(proxy):
                    self._record.remove(proxy)
                    logger.info("代理 %s 连续失败次数过多, 已拉黑", proxy)
                logger.info("代理 %s 请求失败 (尝试 %d/%d): %s", proxy, attempt + 1, max_retries, e)

        if last_exc:
            raise last_exc
        raise RuntimeError("无可用代理")
