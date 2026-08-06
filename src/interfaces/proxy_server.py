"""pproxy 本地代理服务器封装（interfaces 层）

将隐藏池（代理队列）同步为 pproxy 的上游 rserver 列表，实现本地 HTTP/SOCKS4/SOCKS5
正向代理服务器。rserver 是同一列表引用，运行中增删对 schedule 实时生效，
配合 pproxy 的 rc（random）调度——每次请求随机选存活代理，坏代理有概率被跳过，
避免 fa（first_available）固定命中第一个坏代理导致持续失败（见 __init__），实现无缝切换。
"""

import asyncio
import logging

import pproxy

logger = logging.getLogger(__name__)


def _pproxy_uri(proxy: str) -> str:
    """将池内代理字符串转换为 pproxy 上游 URI

    http:// 与 socks5:// 直接可用；https:// 是 HTTPS 代理（代理本身走 TLS），
    pproxy 用 http+ssl:// 前缀表示。
    """
    if proxy.startswith("https://"):
        # pproxy 用 http+ssl:// 表示 HTTPS 代理上游（TLS 到代理）
        return f"http+ssl://{proxy[len('https://'):]}"
    return proxy


class PproxyServer:
    """pproxy 本地代理服务器封装：管理上游 rserver 的动态同步"""

    def __init__(self, listen_uri: str, *, salgorithm: str = "rc") -> None:
        """
        Args:
            listen_uri: 本地监听 URI，如 "http+socks4+socks5://127.0.0.1:8080"
            salgorithm: 上游调度算法（fa=first_available, rr=轮询, rc=随机, lc=最少连接）。
                        默认 rc（随机）：每次请求随机选存活代理，坏代理有概率被跳过，
                        避免 fa 固定用第一个坏代理导致持续失败。
        """
        self._listen_uri = listen_uri
        self._salgorithm = salgorithm
        self._server = pproxy.Server(listen_uri)
        self._handler = None
        self._rserver: list = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, proxies: list[str]) -> None:
        """启动服务器，用给定代理初始化上游列表"""
        self._rserver = [pproxy.Connection(_pproxy_uri(p)) for p in proxies]
        self._loop = asyncio.get_running_loop()
        args = dict(rserver=self._rserver, verbose=lambda *a: None, s=self._salgorithm)
        self._handler = await self._server.start_server(args)
        logger.info("代理服务器已启动: %s, 上游 %d 个代理, 调度 %s", self._listen_uri, len(self._rserver), self._salgorithm)

    def sync(self, proxies: list[str]) -> None:
        """将 rserver 同步到给定代理列表（运行中增删，schedule 实时可见）

        需在服务器启动后、事件循环同线程调用。
        """
        if self._handler is None:
            raise RuntimeError("服务器未启动")
        target = {_pproxy_uri(p) for p in proxies}
        current = {getattr(o, "bind", None) for o in self._rserver}

        # 移除已不在目标中的上游
        for obj in list(self._rserver):
            if getattr(obj, "bind", None) not in target:
                self._rserver.remove(obj)
        # 追加新增的上游
        current_binds = {getattr(o, "bind", None) for o in self._rserver}
        for uri in sorted(target - current_binds):
            self._rserver.append(pproxy.Connection(uri))

        logger.info("上游同步: %d 个代理", len(self._rserver))

    async def close(self) -> None:
        """关闭服务器"""
        if self._handler is not None:
            self._handler.close()
            await self._handler.wait_closed()
            self._handler = None
            logger.info("代理服务器已关闭")
