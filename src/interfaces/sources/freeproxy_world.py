"""FreeProxy.World 代理源（https://www.freeproxy.world/）

多阶段爬取（数据拉取职责，写缓存由 FetchProxiesUseCase 统一做）：
    Phase1 直连拉 HTTPS 代理第 1 页（获取中转代理 + 总页数）
    Phase2 用 HTTPS 代理多线程爬取 HTTPS 各页
    Phase3 用 HTTPS 中转代理拉取各类型（http/https/socks5）代理第 1 页
    Phase4 多线程爬取各类型后续页

公共原语：
    _try_first_page  第 1 页请求：重试直至成功（Phase1/3 复用，直连回退合一）
    _crawl_pages     调度器爬取页面集合（Phase2/4 复用，含未用代理重试轮）
顶层 fetch_proxies 为同步入口（ProxySource 端口），内部 asyncio.run 执行
异步爬取。FreeProxyWorldFetcher.fetch_proxies 返回去重后的 HTTP/HTTPS/SOCKS5
代理列表（带 scheme 前缀），由拉取用例按 scheme 分拣入 freeproxy-world-{scheme} 组。

FpwConfig（并发/限流/超时/请求头参数）由组合根（container，framework 层）
从 config.toml 组装后经 create_source 构造注入，本模块不依赖框架层；
默认值仅作兜底（模块级辅助函数被直接调用/单测的场景）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from functools import partial

import urllib3

import requests as py_requests

from src.domain.models import FpwConfig
from src.domain.ports import ProxySource
from src.interfaces import http_client
from src.interfaces.scheduler import ProxyScheduler
from src.interfaces.sources.base import HttpSourceFetcher
from src.interfaces.ua_generator import UAGenerator

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.freeproxy.world/"

logger = logging.getLogger(__name__)


def _build_headers(ua_gen, seed, config: FpwConfig):
    """构建浏览器化请求头（UA + Sec-Fetch 等）"""
    headers = ua_gen.get_headers(seed)
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": config.accept_language,
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    return headers


def _validate_response(text):
    """页面有效性校验（CF 墙或空页返回 False）"""
    return '<td style="font-weight: 500;">' in text


def _parse_proxies(html):
    """从页面 HTML 解析 (ip, port) 代理列表"""
    ips = re.findall(r'<td style="font-weight: 500;">(.+?)</td>', html)
    ports = re.findall(r'<a href="/\?port=(\d+)', html)
    return [f"{ip}:{port}" for ip, port in zip(ips, ports)]


def _parse_total_pages(html):
    """解析分页总数"""
    pages = re.findall(r'page=(\d+)" class="page-link', html)
    return max(int(p) for p in pages) if pages else 1


async def _fetch_page(url, headers, proxies_dict=None, timeout=None, config: FpwConfig | None = None):
    """请求页面并校验，返回 (html, 耗时秒)

    走代理时用 requests（https 代理需 TLS 到代理）；直连用 http_client。
    免费代理多自签证书，代理链路 verify=False 不校验其证书（与项目 http_client 默认一致，
    校验会误杀全部代理，见 http_client.get 说明）。
    网络请求经线程池（run_in_executor）投递，不阻塞事件循环。
    """
    cfg = config or FpwConfig()
    req_timeout = timeout if timeout is not None else cfg.request_timeout
    t0 = time.time()
    loop = asyncio.get_running_loop()
    # run_in_executor 不转发 kwargs，须用 partial 包装目标调用
    if proxies_dict:
        resp = await loop.run_in_executor(None, partial(
            py_requests.get, url, headers=headers, proxies=proxies_dict, timeout=req_timeout, verify=False,
        ))
    else:
        resp = await loop.run_in_executor(None, partial(
            http_client.get, url, headers=headers, timeout=req_timeout,
        ))
    try:
        elapsed = time.time() - t0
        resp.raise_for_status()
        html = resp.text
        if not _validate_response(html):
            raise ValueError("响应无有效数据(CF墙或空页)")
        return html, elapsed
    finally:
        resp.close()


async def _try_first_page(url, ua_gen, proxies_dict, seed, retries, timeout, phase,
                          config: FpwConfig | None = None):
    """请求第 1 页并解析，重试直至成功；全失败返回 None

    走代理时 proxies_dict 非 None（https 中转），直连传 None 回退；
    两者共用同一重试循环（Phase1/Phase3 复用）。
    seed 为 UA 确定性种子（代理地址；直连用 "local"）。
    """
    cfg = config or FpwConfig()
    for attempt in range(retries):
        try:
            headers = _build_headers(ua_gen, seed, cfg)
            html, elapsed = await _fetch_page(url, headers, proxies_dict, timeout=timeout, config=cfg)
            proxies = _parse_proxies(html)
            total_pages = _parse_total_pages(html)
            logger.info("[%s] 成功: 耗时 %.1fs, 获取 %d 个代理, 共 %d 页",
                        phase, elapsed, len(proxies), total_pages)
            return proxies, total_pages
        except Exception as e:
            logger.warning("[%s] 请求失败 (尝试 %d/%d): %s", phase, attempt + 1, retries, e)
    return None


async def _crawl_pages(proxies, pages, do_fetch, *, phase_name, on_page=None, retry_unused=False,
                       config: FpwConfig | None = None):
    """用代理多线程爬取页面集合（Phase2/4 复用）

    构造调度器 -> 顺序分配页面 -> 构建任务 -> run -> 收集结果；
    失败页面在 retry_unused 时用未分配代理再爬一轮。

    Args:
        proxies:       可用代理列表
        pages:         待爬页面号列表
        do_fetch:      async (proxy, page) -> result
        phase_name:    日志阶段名
        on_page:       成功页回调 (proxy, page, result)，用于收集页面解析内容
        retry_unused:  失败页用未使用代理重试一轮（Phase2 用）
        config:        FpwConfig（并发/限流/重试阈值），缺省用默认配置

    Returns:
        (working, failed, unused): 成功代理 / 丢弃代理 / 未分配代理
    """
    cfg = config or FpwConfig()
    if not pages:
        logger.info("[%s] 只有1页，无需爬取", phase_name)
        return [], [], list(proxies)

    logger.info("[%s] 需要爬取 %d 页, 可用代理 %d 个", phase_name, len(pages), len(proxies))

    scheduler = ProxyScheduler(proxies, max_concurrent=cfg.max_concurrent,
                               max_consecutive_fails=cfg.proxy_retry, task_interval=cfg.task_interval)
    assignments, unassigned = scheduler.assign_round_robin(pages)
    tasks = [(p, pg) for p, pgs in assignments.items() for pg in pgs]
    logger.info("[%s] 第一轮: 分配 %d 个代理, 未使用 %d 个, 无代理分配 %d 页",
                phase_name, len(assignments), len(scheduler.unused), len(unassigned))

    results = await scheduler.run(tasks, phase_name, do_fetch, on_success=on_page)
    failed_pages = [item for r in results.values() for item in r["failed_items"]]
    failed_pages.extend(unassigned)

    failed_proxies = set(scheduler.failed)
    if retry_unused and failed_pages and scheduler.unused:
        # 第二轮：用未使用代理重爬失败页面（互斥的代理集合，结果直接并入）
        retry_scheduler = ProxyScheduler(scheduler.unused, max_concurrent=cfg.max_concurrent,
                                         max_consecutive_fails=cfg.proxy_retry, task_interval=cfg.task_interval)
        retry_assignments, _ = retry_scheduler.assign_round_robin(failed_pages)
        retry_tasks = [(p, pg) for p, pgs in retry_assignments.items() for pg in pgs]
        retry_results = await retry_scheduler.run(retry_tasks, f"{phase_name}重试", do_fetch, on_success=on_page)
        for p, r in retry_results.items():
            results.setdefault(p, {"success": False, "failed_items": []}).update(r)
        failed_proxies.update(retry_scheduler.failed)
        logger.info("[%s] 重试轮: 新增可用 %d 个代理",
                    phase_name, sum(1 for r in retry_results.values() if r["success"]))

    working = [p for p, r in results.items() if r["success"]]
    logger.info("[%s] 最终: 可用 %d, 失败 %d, 未使用 %d",
                phase_name, len(working), len(failed_proxies), len(scheduler.unused))
    return working, list(failed_proxies), scheduler.unused


async def _crawl_type(ua_gen, working_proxies, unused_proxies, proxy_type, retries, timeout,
                      config: FpwConfig | None = None):
    """爬取指定类型代理：第1页（中转代理逐个尝试 + 直连回退）+ 多线程后续页，返回去重列表"""
    cfg = config or FpwConfig()
    url = f"{BASE_URL}?type={proxy_type}&page=1"

    # Phase3: 逐个 HTTPS 中转代理尝试，全部失败回退直连（共用重试循环）
    first = None
    for proxy in working_proxies:
        result = await _try_first_page(
            url, ua_gen, {"https": f"https://{proxy}"}, proxy, retries, timeout, f"Phase3-{proxy_type}", cfg,
        )
        if result is not None:
            first = result
            break
    if first is None:
        result = await _try_first_page(url, ua_gen, None, "local", retries, timeout, f"Phase3-{proxy_type}", cfg)
        first = result or ([], 0)
    first_proxies, total_pages = first
    if not first_proxies:
        logger.warning("Phase3 失败，无%s代理", proxy_type)
        return []

    # Phase4: 多线程爬取后续页，逐页解析追加
    more: list[str] = []
    pages = list(range(2, total_pages + 1))
    if pages and (working_proxies or unused_proxies):
        available = working_proxies + unused_proxies

        def on_page(proxy, page, result):
            html, _ = result
            more.extend(_parse_proxies(html))

        async def do_fetch(proxy, page):
            headers = _build_headers(ua_gen, proxy, cfg)
            proxies_dict = {"https": f"https://{proxy}"}
            return await _fetch_page(f"{BASE_URL}?type={proxy_type}&page={page}",
                                     headers, proxies_dict, timeout=timeout, config=cfg)

        await _crawl_pages(available, pages, do_fetch, phase_name=f"Phase4-{proxy_type}", on_page=on_page, config=cfg)

    return list(dict.fromkeys(first_proxies + more))


class FreeProxyWorldFetcher:
    """FreeProxy.World 代理源：多阶段爬取（ProxySource 端口实现，运行参数经构造注入）"""

    def __init__(self, config: FpwConfig | None = None) -> None:
        self._config = config or FpwConfig()

    def fetch_proxies(self, retries=None, timeout=None) -> list[str]:
        """执行多阶段爬取，返回去重后的 HTTP/HTTPS/SOCKS5 代理列表（带 scheme 前缀）

        Phase1/2 获取可用的 HTTPS 中转代理（working/unused 一并收集入池，failed 丢弃），
        Phase3/4 用其中转爬取三类代理页。同步入口，内部事件循环执行异步爬取。

        retries:  各页面请求的重试次数（默认 2）
        timeout:  各页面请求的超时秒数（默认 config.request_timeout）

        Windows 上用 SelectorEventLoop 而非默认 ProactorEventLoop：本源大量
        run_in_executor 线程池 + SSL 请求，ProactorEventLoop(IOCP) 在此场景下可能
        静默退出导致进程终止；SelectorEventLoop 基于 select，更稳定。本源只用
        线程池（网络请求），不需要 IOCP 的 subprocess 等能力。
        """
        if sys.platform == "win32":
            loop = asyncio.SelectorEventLoop()
            try:
                return loop.run_until_complete(self._fetch_proxies_async(retries, timeout))
            finally:
                loop.close()
        return asyncio.run(self._fetch_proxies_async(retries, timeout))

    async def _fetch_proxies_async(self, retries, timeout) -> list[str]:
        """多阶段爬取核心（async 实现，同步入口封装事件循环）"""
        cfg = self._config
        req_retries = retries if retries is not None else 2
        req_timeout = timeout if timeout is not None else cfg.request_timeout
        ua_gen = UAGenerator()

        # Phase1: 直连拉 HTTPS 代理第 1 页（获取中转代理 + 总页数）
        result = await _try_first_page(
            f"{BASE_URL}?type=https&page=1", ua_gen, None, "local", req_retries, req_timeout, "Phase1", cfg,
        )
        if result is None:
            logger.warning("Phase1 失败，无HTTPS代理")
            return []
        https_proxies, total_pages = result

        # Phase2: 用 HTTPS 代理多线程爬取 HTTPS 2..N 页（失败页用未使用代理重试）
        async def do_fetch_https(proxy, page):
            headers = _build_headers(ua_gen, proxy, cfg)
            proxies_dict = {"https": f"https://{proxy}"}
            return await _fetch_page(f"{BASE_URL}?type=https&page={page}",
                                     headers, proxies_dict, timeout=req_timeout, config=cfg)

        working, failed, unused = await _crawl_pages(
            https_proxies, list(range(2, total_pages + 1)),
            do_fetch_https, phase_name="Phase2", retry_unused=True, config=cfg,
        )

        # Phase1/2 中转 HTTPS 代理入池（已验证可用的 working + 未使用的 unused）
        all_proxies = HttpSourceFetcher._with_protocol(working + unused, protocol="https")

        for proxy_type in ("http", "https", "socks5"):
            type_proxies = await _crawl_type(ua_gen, working, unused, proxy_type, req_retries, req_timeout, cfg)
            all_proxies.extend(HttpSourceFetcher._with_protocol(type_proxies, protocol=proxy_type))
            logger.info("获取 %d 个%s代理", len(type_proxies), proxy_type)

        all_proxies = list(dict.fromkeys(all_proxies))
        logger.info("总计获取 %d 个代理", len(all_proxies))
        return all_proxies


def create_source(config: FpwConfig | None = None) -> ProxySource:
    """加载器入口：返回本源实例（FPW 参数由组合根注入，缺省用默认配置）"""
    return FreeProxyWorldFetcher(config)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    proxies = FreeProxyWorldFetcher().fetch_proxies()
    print(f"\n共获取 {len(proxies)} 个代理")
