"""CLI 命令处理（根目录 cli.py 为薄入口，经此 main() 分发）

命令经组合根（build_container）获取依赖；模块级 _get_container() 为测试缝，
测试可 patch 它返回 fake container。
"""

import argparse
import asyncio
import json
import logging
import sys
import time

from src.domain.models import normalize_proxy, to_proxy_dict
from src.framework import config
from src.framework.container import build_container
from src.framework.logging import setup_logging
from src.interfaces import http_client

logger = logging.getLogger(__name__)

_container = None


def _get_container():
    """懒加载组合根（测试通过 patch 此函数注入 fake container）"""
    global _container
    if _container is None:
        _container = build_container()
    return _container


def cmd_fetch(args):
    c = _get_container()
    try:
        c.fetch.execute()
        available = c.pool.get_available_proxies()
        _print_proxies(list(available.keys()), args.json)
    finally:
        c.save_full()


def cmd_list(args):
    c = _get_container()
    group = args.group
    available = c.pool.get_available_proxies()
    if group:
        proxies = [p for p, info in available.items() if info["group"] == group]
    else:
        proxies = list(available.keys())
    _print_proxies(proxies, args.json)


def cmd_get(args):
    c = _get_container()
    try:
        proxies = c.get_proxies.execute(args.n)
        _print_proxies(proxies, args.json)
    finally:
        c.save_full()


def cmd_status(args):
    c = _get_container()
    pool = c.pool
    print("代理池状态:")
    print(f"  {'组名':<20} {'数量':>6} {'TTL':>10}")
    print(f"  {'-'*20} {'-'*6} {'-'*10}")
    for name in pool.proxy_groups:
        ttl = pool.proxy_groups[name]["ttl"]
        ttl_str = "永不过期" if ttl is None else f"{ttl}s"
        active = len(pool.get_active_proxies(name))
        print(f"  {name:<20} {active:>6} {ttl_str:>10}")
    print()
    available = pool.get_available_proxies()
    print(f"  可用代理总数: {len(available)}")
    print()
    if pool.source_paused:
        print("  暂停的代理源:")
        # 遍历副本列表：source_pause_remaining 对过期源会删除记录
        for src in list(pool.source_paused):
            remaining = pool.source_pause_remaining(src)
            if remaining is None:
                continue
            print(f"    {src} (剩余 {remaining}s)")
    else:
        print("  无暂停的代理源")


def cmd_blacklist(args):
    c = _get_container()
    try:
        for proxy in args.proxies:
            proxy = normalize_proxy(proxy)
            c.record.remove(proxy)
            print(f"已加入黑名单: {proxy}")
    finally:
        c.save_full()


def cmd_trust(args):
    c = _get_container()
    try:
        for proxy in args.proxies:
            proxy = normalize_proxy(proxy)
            c.record.trust(proxy)
            print(f"已加入信任池: {proxy}")
    finally:
        c.save_full()


def cmd_request(args):
    c = _get_container()
    try:
        try:
            proxy, resp = c.request_with_retry.execute(args.url, max_retries=args.retries, timeout=args.timeout)
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
        c.save_full()


def cmd_test(args):
    if args.concurrent or not args.proxy:
        c = _get_container()
        available = c.pool.get_available_proxies()
        proxies = list(available.keys())[:args.concurrent or len(available)]
        if not proxies:
            print("错误: 无可用代理", file=sys.stderr)
            sys.exit(1)
        print(f"并发测试 {len(proxies)} 个代理访问 {args.url}")
        results = asyncio.run(c.check_proxies.execute(
            proxies, args.url, max_concurrent=args.concurrent or 8, timeout=args.timeout,
        ))
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
        proxy = normalize_proxy(args.proxy)
        proxy_dict = to_proxy_dict(proxy)
        url = args.url
        t0 = time.time()
        try:
            resp = http_client.get(url, proxies=proxy_dict, timeout=args.timeout)
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
    """打印代理列表：--json 输出原始 JSON；否则输出带序号的对齐表格"""
    if as_json:
        print(json.dumps(proxies, ensure_ascii=False))
        return
    if not proxies:
        return
    # 序号列按总数动态取宽，代理列按最长项取宽，保证对齐
    num_width = len(str(len(proxies)))
    proxy_width = max(len(p) for p in proxies)
    print(f"  {'#':>{num_width}}  代理")
    print(f"  {'-' * num_width}  {'-' * proxy_width}")
    for i, p in enumerate(proxies, 1):
        print(f"  {i:>{num_width}}  {p}")


def cmd_serve(args):
    """启动本地代理服务器：pproxy 监听 + 代理服务队列心跳"""
    from src.framework.serve import run

    # serve 用 CLI 的 host/port 重建组合根（proxy_server 按监听地址构造）
    c = build_container()
    allowed = set(args.type) if args.type else set(config.SERVE_PROXY_TYPES)
    print(f"启动本地代理服务器: {args.host}:{args.port} (类型: {','.join(sorted(allowed))})")
    run(
        c,
        host=args.host,
        port=args.port,
        capacity=args.queue_size,
        interval=args.interval,
        probe_url=args.probe_url,
        allowed_types=allowed,
        concurrency=config.HEARTBEAT_CONCURRENCY,
        web_host=args.web_host,
        web_port=args.web_port,
    )


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
    p_list.add_argument("--group", choices=list(config.PROXY_GROUPS.keys()), help="按组筛选")
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
    p_test.add_argument("--concurrent", "-c", type=int, default=0, help="并发测试池中N个代理 (0=池中全部；同时指定 proxy 时忽略此参数，走单代理模式)")

    # serve
    p_serve = sub.add_parser("serve", help="启动本地代理服务器 (HTTP/SOCKS4/SOCKS5)")
    p_serve.add_argument("--host", default=config.SERVER_HOST, help=f"监听地址 (默认{config.SERVER_HOST})")
    p_serve.add_argument("--port", type=int, default=config.SERVER_PORT, help=f"监听端口 (默认{config.SERVER_PORT})")
    p_serve.add_argument("--queue-size", type=int, default=config.STANDBY_QUEUE_CAPACITY, help=f"代理服务队列容量 (默认{config.STANDBY_QUEUE_CAPACITY})")
    p_serve.add_argument("--interval", type=int, default=config.HEARTBEAT_INTERVAL, help=f"心跳间隔秒 (默认{config.HEARTBEAT_INTERVAL})")
    p_serve.add_argument("--probe-url", default=config.HEARTBEAT_PROBE_URL, help="心跳探测URL")
    p_serve.add_argument("--type", action="append", choices=["http", "https", "socks5"],
                         help="允许的代理类型 (可多次指定)，默认全部")
    p_serve.add_argument("--web-host", default=config.WEB_HOST, help=f"Web 面板监听地址 (默认{config.WEB_HOST})")
    p_serve.add_argument("--web-port", type=int, default=config.WEB_PORT, help=f"Web 面板监听端口 (默认{config.WEB_PORT})")

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
        "serve": cmd_serve,
    }

    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
