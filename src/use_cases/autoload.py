"""autoload 用例：启动时从清单文件加载代理到代理服务队列（固定、跳过已存在）

清单格式：一行一个代理地址，# 为行内注释（# 及之后忽略），空行/纯注释行跳过。
裸 ip:port 补默认 http:// scheme（复用 normalize_proxy）。

加载策略：
    - 代理加入代理服务队列组（standby_group，serve 的 pproxy 上游集合）
    - 标记 pinned（固定）：在代理服务队列排最前，不被心跳踢出/拉黑
    - 跳过池中已存在的代理（保留原有 consecutive_fails/latency 等状态，不覆盖）
    - 文件内重复代理去重（保序，仅首个生效）

本用例为纯逻辑（解析 + 入池），不读文件——文件 IO 由 framework 层
（container.build_container）完成，内容以 str 注入，便于单元测试。
"""

from __future__ import annotations

import logging
import time

from src.domain.models import normalize_proxy
from src.domain.proxy_pool import ProxyPool

logger = logging.getLogger(__name__)


def _parse(content: str) -> list[str]:
    """解析清单内容：一行一个代理，# 行内注释，跳过空行/纯注释，文件内去重保序

    # 出现在行内任意位置均视为注释起点（代理 URL 不含 #，无冲突）。
    裸 ip:port 经 normalize_proxy 补 http:// scheme。
    """
    seen: set[str] = set()
    proxies: list[str] = []
    for line in content.splitlines():
        # # 行内注释：取 # 之前部分作为代理地址
        addr = line.split("#", 1)[0].strip()
        if not addr:
            continue
        proxy = normalize_proxy(addr)
        if proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


class AutoloadUseCase:
    """从清单内容加载代理到代理服务队列（固定、跳过已存在）"""

    def __init__(self, *, pool: ProxyPool, standby_group: str) -> None:
        """
        Args:
            pool:           代理池领域模型
            standby_group:  代理服务队列组名（autoload 代理入此组并标记 pinned）
        """
        self._pool = pool
        self._standby_group = standby_group

    def execute(self, content: str) -> tuple[int, int]:
        """解析清单内容并加载到代理服务队列，返回 (加载数, 跳过已存在数)

        已存在于任意组的代理跳过（含黑名单/信任池/代理服务队列等，不覆盖原状态）；
        过期代理 find_proxy 会自动清除并视为不存在，故会被重新加载。
        """
        proxies = _parse(content)
        if not proxies:
            logger.info("autoload: 清单为空，无代理加载")
            return 0, 0
        loaded = 0
        skipped = 0
        now = int(time.time())
        for proxy in proxies:
            # find_proxy 遍历所有组：已存在则跳过（保留原状态），过期代理自动清除视为不存在
            group, _ = self._pool.find_proxy(proxy)
            if group is not None:
                skipped += 1
                continue
            self._pool.add_to_group(self._standby_group, proxy, pinned=True, pinned_at=now)
            loaded += 1
        logger.info("autoload: 加载 %d 个代理, 跳过 %d 个已存在", loaded, skipped)
        return loaded, skipped
