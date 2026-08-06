"""代理池状态 JSON 文件存储（domain.ports.ProxyStore 实现）

只负责读写，领域规则（过期过滤）在 ProxyPool.export_state/restore_state。
数据格式与旧 .cache 文件兼容。
"""

import json
import os


class JsonProxyStore:
    """代理池状态 JSON 文件存储"""

    def __init__(self, cache_file: str) -> None:
        self._cache_file = cache_file

    def load(self) -> dict:
        """读取状态；文件不存在或损坏时返回空 dict"""
        if not os.path.exists(self._cache_file):
            return {}
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, state: dict) -> None:
        """写入状态（紧凑 JSON，ensure_ascii=False）"""
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
