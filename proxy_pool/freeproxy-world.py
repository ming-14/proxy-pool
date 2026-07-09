import json
import logging
import os
import re
import sys
import time
import requests
import urllib3
from urllib.parse import urlencode

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log import setup_logging
setup_logging()
from ua_generator import UAGenerator
from config import CACHE_FILE, FPW_MAX_CONCURRENT, FPW_PROXY_RETRY, FPW_TASK_INTERVAL, FPW_REQUEST_TIMEOUT
from scheduler import ProxyScheduler

BASE_URL = "https://www.freeproxy.world/"

logger = logging.getLogger(__name__)



def _build_headers(ua_gen, seed):
    headers = ua_gen.get_headers(seed)
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    return headers


def _validate_response(text):
    return '<td style="font-weight: 500;">' in text


def _parse_proxies(html):
    ips = re.findall(r'<td style="font-weight: 500;">(.+?)</td>', html)
    ports = re.findall(r'<a href="/\?port=(\d+)', html)
    return [f"{ip}:{port}" for ip, port in zip(ips, ports)]


def _parse_total_pages(html):
    pages = re.findall(r'page=(\d+)" class="page-link', html)
    return max(int(p) for p in pages) if pages else 1


def _fetch_page(url, headers, proxies_dict=None, timeout=FPW_REQUEST_TIMEOUT):
    t0 = time.time()
    resp = requests.get(url, headers=headers, proxies=proxies_dict, timeout=timeout, verify=False)
    elapsed = time.time() - t0
    resp.raise_for_status()
    html = resp.text
    if not _validate_response(html):
        raise ValueError("响应无有效数据(CF墙或空页)")
    return html, elapsed


def phase1_fetch_https(ua_gen):
    headers = _build_headers(ua_gen, "local")
    for attempt in range(2):
        try:
            logger.info("[Phase1] 直连请求 HTTPS 代理第1页 (尝试 %d/2)", attempt + 1)
            html, elapsed = _fetch_page(f"{BASE_URL}?type=https&page=1", headers)
            proxies = _parse_proxies(html)
            total_pages = _parse_total_pages(html)
            logger.info("[Phase1] 成功: 耗时 %.1fs, 获取 %d 个HTTPS代理, 共 %d 页", elapsed, len(proxies), total_pages)
            return proxies, total_pages
        except Exception as e:
            logger.warning("[Phase1] 直连失败 (尝试 %d/2): %s", attempt + 1, e)
    logger.warning("[Phase1] 直连全部失败，无法继续")
    return [], 0


def phase2_crawl_https(ua_gen, https_proxies, total_pages):
    if total_pages <= 1:
        logger.info("[Phase2] 只有1页，无需爬取")
        return [], [], https_proxies

    pages = list(range(2, total_pages + 1))
    logger.info("[Phase2] 需要爬取 %d 页 (2..%d), 可用代理 %d 个", len(pages), total_pages, len(https_proxies))

    scheduler = ProxyScheduler(https_proxies, max_concurrent=FPW_MAX_CONCURRENT, max_fails=FPW_PROXY_RETRY, task_interval=FPW_TASK_INTERVAL)

    assignments, unassigned = scheduler.assign_sequential(pages)
    logger.info("[Phase2] 第一轮: 分配 %d 个代理, 未使用 %d 个, 无代理分配 %d 页",
                 len(assignments), len(scheduler.unused), len(unassigned))

    tasks = []
    for proxy, proxy_pages in assignments.items():
        for page in proxy_pages:
            tasks.append((proxy, page))

    def do_fetch(proxy, page):
        url = f"{BASE_URL}?type=https&page={page}"
        headers = _build_headers(ua_gen, proxy)
        proxies_dict = {"https": f"https://{proxy}"}
        return _fetch_page(url, headers, proxies_dict)

    results = scheduler.run(tasks, "Phase2", do_fetch)

    working = [p for p, r in results.items() if r["success"]]
    failed_pages = []
    for p, r in results.items():
        failed_pages.extend(r["failed_items"])
    failed_pages.extend(unassigned)

    logger.info("[Phase2] 第一轮结果: 可用 %d, 失败页面 %d", len(working), len(failed_pages))

    if failed_pages and scheduler.unused:
        retry_scheduler = ProxyScheduler(scheduler.unused, max_concurrent=FPW_MAX_CONCURRENT, max_fails=FPW_PROXY_RETRY, task_interval=FPW_TASK_INTERVAL)
        retry_assignments, _ = retry_scheduler.assign_sequential(failed_pages)
        retry_tasks = []
        for proxy, proxy_pages in retry_assignments.items():
            for page in proxy_pages:
                retry_tasks.append((proxy, page))

        retry_results = retry_scheduler.run(retry_tasks, "Phase2重试", do_fetch)
        working.extend(p for p, r in retry_results.items() if r["success"])
        scheduler._failed.update(retry_scheduler.failed)

    logger.info("[Phase2] 最终: 可用 %d, 失败 %d, 未使用 %d",
                 len(working), len(scheduler.failed), len(scheduler.unused))
    return working, list(scheduler.failed), scheduler.unused


def phase3_fetch_http(ua_gen, working_proxies):
    url = f"{BASE_URL}?type=http&page=1"

    for proxy in working_proxies:
        for attempt in range(FPW_PROXY_RETRY + 1):
            try:
                headers = _build_headers(ua_gen, proxy)
                proxies_dict = {"https": f"https://{proxy}"}
                logger.info("[Phase3] 用 %s 请求 HTTP 代理第1页 (尝试 %d/%d)",
                             proxy, attempt + 1, FPW_PROXY_RETRY + 1)
                html, elapsed = _fetch_page(url, headers, proxies_dict)
                http_proxies = _parse_proxies(html)
                total_pages = _parse_total_pages(html)
                logger.info("[Phase3] 成功: 耗时 %.1fs, 获取 %d 个HTTP代理, 共 %d 页",
                             elapsed, len(http_proxies), total_pages)
                return http_proxies, total_pages
            except Exception as e:
                logger.warning("[Phase3] 代理 %s 失败 (尝试 %d/%d): %s",
                             proxy, attempt + 1, FPW_PROXY_RETRY + 1, e)

    logger.info("[Phase3] 所有HTTPS代理失败，尝试直连获取HTTP代理第1页")
    for attempt in range(2):
        try:
            headers = _build_headers(ua_gen, "local")
            html, elapsed = _fetch_page(url, headers)
            http_proxies = _parse_proxies(html)
            total_pages = _parse_total_pages(html)
            logger.info("[Phase3] 直连成功: 耗时 %.1fs, 获取 %d 个HTTP代理, 共 %d 页",
                         elapsed, len(http_proxies), total_pages)
            return http_proxies, total_pages
        except Exception as e:
            logger.warning("[Phase3] 直连失败 (尝试 %d/2): %s", attempt + 1, e)

    logger.warning("[Phase3] 全部失败")
    return [], 0


def phase4_crawl_http(ua_gen, working_proxies, unused_proxies, total_pages):
    if total_pages <= 1:
        logger.info("[Phase4] 只有1页，无需爬取")
        return []

    pages = list(range(2, total_pages + 1))
    available = working_proxies + unused_proxies
    logger.info("[Phase4] 需要爬取 %d 页 (2..%d), 可用代理 %d 个", len(pages), total_pages, len(available))

    if not available:
        logger.warning("[Phase4] 无可用代理")
        return []

    scheduler = ProxyScheduler(available, max_concurrent=FPW_MAX_CONCURRENT, max_fails=FPW_PROXY_RETRY, task_interval=FPW_TASK_INTERVAL)
    assignments, _ = scheduler.assign_round_robin(pages)

    tasks = []
    for proxy, proxy_pages in assignments.items():
        for page in proxy_pages:
            tasks.append((proxy, page))

    all_proxies = []

    def on_success(proxy, page, result):
        html, elapsed = result
        page_proxies = _parse_proxies(html)
        all_proxies.extend(page_proxies)

    def do_fetch(proxy, page):
        url = f"{BASE_URL}?type=http&page={page}"
        headers = _build_headers(ua_gen, proxy)
        proxies_dict = {"https": f"https://{proxy}"}
        return _fetch_page(url, headers, proxies_dict)

    scheduler.run(tasks, "Phase4", do_fetch, on_success=on_success)

    logger.info("[Phase4] 完成: 共获取 %d 个HTTP代理", len(all_proxies))
    return all_proxies


def phase5_write_cache(http_proxies):
    from cache import CacheManager

    cm = CacheManager(cache_file=CACHE_FILE)
    new_count = 0
    for p in http_proxies:
        if cm.find_proxy(p)[0] is not None or cm.is_blacklisted(p):
            continue
        cm.add_to_group("freeproxy-world", p)
        new_count += 1
    cm.save()
    logger.info("[Phase5] 写入 .cache: 新增 %d 个HTTP代理 (去重后), 路径 %s", new_count, CACHE_FILE)
    return new_count


def fetch_proxies(retries=None, timeout=None):
    ua_gen = UAGenerator()

    https_proxies, n1 = phase1_fetch_https(ua_gen)
    if not https_proxies:
        logger.warning("Phase1 失败，无HTTPS代理")
        return []

    working, failed, unused = phase2_crawl_https(ua_gen, https_proxies, n1)

    http_proxies, n2 = phase3_fetch_http(ua_gen, working)
    if not http_proxies:
        logger.warning("Phase3 失败，无HTTP代理")
        return []

    more_http = phase4_crawl_http(ua_gen, working, unused, n2)
    all_http = list(dict.fromkeys(http_proxies + more_http))

    logger.info("总计获取 %d 个HTTP代理", len(all_http))
    return all_http


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    proxies = fetch_proxies()
    phase5_write_cache(proxies)
    print(f"\n共获取 {len(proxies)} 个 HTTP 代理")
