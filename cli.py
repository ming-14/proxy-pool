import argparse
import json
import logging
import sys
import os
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cache import CacheManager
from config import MAX_FAILS, TRUSTED_MAX_FAILS
from manager import ProxyManager
from scheduler import request_with_retry, check_proxies_concurrent
from log import setup_logging


def cmd_fetch(args):
    pm = ProxyManager()
    try:
        pm._fetch_proxies()
        available = pm._cache.get_available_proxies()
        _print_proxies(list(available.keys()), args.json)
    finally:
        pm.save_cache()


def cmd_list(args):
    cm = CacheManager()
    group = args.group
    available = cm.get_available_proxies()
    if group:
        proxies = [p for p, info in available.items() if info["group"] == group]
    else:
        proxies = list(available.keys())
    _print_proxies(proxies, args.json)


def cmd_get(args):
    pm = ProxyManager()
    try:
        proxies = pm.get_proxies(args.n)
        _print_proxies(proxies, args.json)
    finally:
        pm.save_cache()


def cmd_status(args):
    cm = CacheManager()
    groups = cm.proxy_groups
    print("代理池状态:")
    print(f"  {'组名':<20} {'数量':>6} {'TTL':>10}")
    print(f"  {'-'*20} {'-'*6} {'-'*10}")
    for name, group in groups.items():
        ttl_str = "永不过期" if group["ttl"] is None else f"{group['ttl']}s"
        active = sum(1 for p in group["proxies"] if cm._is_active(name, p))
        print(f"  {name:<20} {active:>6} {ttl_str:>10}")
    print()
    available = cm.get_available_proxies()
    print(f"  可用代理总数: {len(available)}")
    print()
    if cm.source_paused:
        print("  暂停的代理源:")
        now = time.time()
        for src, info in cm.source_paused.items():
            from config import SOURCE_PAUSE_TTL
            ttl = info.get("ttl") or SOURCE_PAUSE_TTL
            remaining = max(0, int(ttl - (now - info["paused_at"])))
            print(f"    {src} (剩余 {remaining}s)")
    else:
        print("  无暂停的代理源")


def cmd_blacklist(args):
    cm = CacheManager()
    for proxy in args.proxies:
        cm.move_proxy(proxy, "blacklist", fails=0)
        print(f"已加入黑名单: {proxy}")
    cm.save()


def cmd_trust(args):
    cm = CacheManager()
    for proxy in args.proxies:
        cm.add_to_group("trusted", proxy, fails=0)
        print(f"已加入信任池: {proxy}")
    cm.save()


def cmd_request(args):
    pm = ProxyManager()
    try:
        try:
            proxy, resp = request_with_retry(pm, args.url, max_retries=args.retries, timeout=args.timeout)
            print(f"使用代理: {proxy}")
            print(f"状态码: {resp.status_code}")
            print(f"编码: {resp.encoding}")
            if args.headers:
                print("响应头:")
                for k, v in resp.headers.items():
                    print(f"  {k}: {v}")
                return
            text = resp.text
            if args.max_length and len(text) > args.max_length:
                text = text[:args.max_length] + f"\n... (截断, 共 {len(resp.text)} 字节)"
            print(text)
        except Exception as e:
            print(f"请求失败: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
    finally:
        pm.save_cache()


def cmd_test(args):
    if args.concurrent or not args.proxy:
        cm = CacheManager()
        available = cm.get_available_proxies()
        proxies = list(available.keys())[:args.concurrent or len(available)]
        if not proxies:
            print("错误: 无可用代理", file=sys.stderr)
            sys.exit(1)
        print(f"并发测试 {len(proxies)} 个代理访问 {args.url}")
        results = check_proxies_concurrent(proxies, args.url, max_concurrent=args.concurrent or 8, timeout=args.timeout)
        success_count = sum(1 for r in results.values() if r["success"])
        fail_count = len(results) - success_count
        print(f"\n结果: {success_count} 成功, {fail_count} 失败\n")
        print(f"  {'代理':<25} {'状态':>6} {'耗时':>8} {'状态码':>6} {'错误'}")
        print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*6} {'-'*20}")
        for proxy, r in sorted(results.items(), key=lambda x: (not x[1]["success"], x[1]["elapsed"])):
            status = "成功" if r["success"] else "失败"
            code = str(r["status_code"]) if r["status_code"] else "-"
            err = r["error"] or ""
            print(f"  {proxy:<25} {status:>6} {r['elapsed']:>7.2f}s {code:>6} {err}")
    else:
        proxy = args.proxy
        proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        url = args.url
        t0 = time.time()
        try:
            resp = requests.get(url, proxies=proxy_dict, timeout=args.timeout)
            elapsed = time.time() - t0
            print(f"成功: {proxy}")
            print(f"状态码: {resp.status_code}")
            print(f"耗时: {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"失败: {proxy}")
            print(f"错误: {type(e).__name__}: {e}")
            print(f"耗时: {elapsed:.2f}s")
            sys.exit(1)


def _print_proxies(proxies, as_json=False):
    if as_json:
        print(json.dumps(proxies, ensure_ascii=False))
    else:
        for p in proxies:
            print(p)


def main():
    parser = argparse.ArgumentParser(
        prog="proxy-pool",
        description="代理池管理工具",
    )
    parser.add_argument("--debug", action="store_true", help="输出详细日志")

    sub = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="从代理源拉取代理")
    p_fetch.add_argument("--json", action="store_true", help="JSON 格式输出")

    # list / ls
    p_list = sub.add_parser("list", aliases=["ls"], help="列出可用代理")
    p_list.add_argument("--group", choices=["default", "freeproxy-world", "trusted"], help="按组筛选")
    p_list.add_argument("--json", action="store_true", help="JSON 格式输出")

    # get
    p_get = sub.add_parser("get", help="随机获取代理")
    p_get.add_argument("-n", type=int, default=1, help="获取数量 (默认1)")
    p_get.add_argument("--json", action="store_true", help="JSON 格式输出")

    # status
    sub.add_parser("status", help="查看代理池状态")

    # blacklist
    p_bl = sub.add_parser("blacklist", help="将代理加入黑名单")
    p_bl.add_argument("proxies", nargs="+", help="代理地址 (ip:port)")

    # trust
    p_trust = sub.add_parser("trust", help="将代理加入信任池")
    p_trust.add_argument("proxies", nargs="+", help="代理地址 (ip:port)")

    # request
    p_req = sub.add_parser("request", help="用代理池中的代理访问URL (自动重试换代理)")
    p_req.add_argument("url", help="目标URL")
    p_req.add_argument("--timeout", type=int, default=10, help="超时秒数 (默认10)")
    p_req.add_argument("--retries", type=int, default=5, help="最大重试次数 (默认5)")
    p_req.add_argument("--max-length", type=int, default=None, help="响应内容最大长度 (字节)")
    p_req.add_argument("--headers", action="store_true", help="仅输出响应头")

    # test
    p_test = sub.add_parser("test", help="测试代理能否访问URL")
    p_test.add_argument("proxy", nargs="?", help="代理地址 (ip:port), 省略则并发测试池中代理")
    p_test.add_argument("url", help="目标URL")
    p_test.add_argument("--timeout", type=int, default=10, help="超时秒数 (默认10)")
    p_test.add_argument("--concurrent", "-c", type=int, default=0, help="并发测试池中N个代理 (0=单代理模式)")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        setup_logging()

    cmd_map = {
        "fetch": cmd_fetch,
        "list": cmd_list,
        "ls": cmd_list,
        "get": cmd_get,
        "status": cmd_status,
        "blacklist": cmd_blacklist,
        "trust": cmd_trust,
        "request": cmd_request,
        "test": cmd_test,
    }

    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
