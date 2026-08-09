"""命令行入口与编排。"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from pspeed import __version__
from pspeed.proxy import parse_many, load_file
from pspeed.nodes import get_download_nodes, get_upload_nodes, DOWNLOAD_NODES, UPLOAD_NODES
from pspeed.probe import probe_one
from pspeed.report import build_report, emit


def _parse_bytes(s: str) -> int:
    s = s.strip().upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1024, s[:-1]
    elif s.endswith("M"):
        mult, s = 1024 * 1024, s[:-1]
    elif s.endswith("G"):
        mult, s = 1024 * 1024 * 1024, s[:-1]
    return int(float(s) * mult)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pspeed",
        description="命令行代理测速工具，支持 http(s)/socks4/socks5，JSON 输出",
    )
    p.add_argument("--proxy", action="append", default=[], metavar="URL",
                   help="代理地址，可多次指定，如 socks5://1.2.3.4:1080")
    p.add_argument("--proxy-file", metavar="PATH",
                   help="从文件批量读取代理，每行一个")
    p.add_argument("--test", default="tcp,ip,download", metavar="LIST",
                   help="测速维度(csv): tcp,ip,download,upload")
    p.add_argument("--download-bytes", default="200M", metavar="N",
                   help="下载测速字节数(支持 K/M/G)")
    p.add_argument("--upload-bytes", default="100M", metavar="N",
                   help="上传测速字节数(支持 K/M/G)")
    p.add_argument("--nodes", default="all", metavar="LIST",
                   help="下载测速节点(csv)或 all")
    p.add_argument("--upload-nodes", default="all", metavar="LIST",
                   help="上传测速节点(csv)或 all")
    p.add_argument("--multi", type=int, default=1, metavar="N",
                   help="每个节点重复测速次数，取峰值")
    p.add_argument("--concurrency", type=int, default=10, metavar="N",
                   help="并发探测数")
    p.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                   help="单次探测硬性总时限(秒)")
    p.add_argument("--tcp-timeout", type=float, default=5.0, metavar="SEC",
                   help="TCP 连通超时(秒)")
    p.add_argument("--min-speed", type=float, default=0.0, metavar="MBPS",
                   help="筛选: 最低下载速度")
    p.add_argument("--max-latency", type=float, default=0.0, metavar="MS",
                   help="筛选: 最高 TCP 延迟(0=不限制)")
    p.add_argument("--pretty", action="store_true", help="JSON 美化缩进输出")
    p.add_argument("--quiet", action="store_true", help="静默，只输出最终 JSON")
    p.add_argument("-v", "--version", action="version", version=f"pspeed {__version__}")
    return p


def collect_proxies(args) -> list:
    items = list(args.proxy)
    if args.proxy_file:
        items.extend(load_file(args.proxy_file))
    return parse_many(items)


def _passes_filter(result: dict, min_speed: float, max_latency: float) -> bool:
    if min_speed > 0:
        dl = result.get("download")
        if not dl or not dl.get("ok") or dl.get("best_mbps", 0) < min_speed:
            return False
    if max_latency > 0:
        tcp = result.get("tcp")
        if not tcp or not tcp.get("connected"):
            return False
        if (tcp.get("latency_ms") or 0) > max_latency:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    proxies = collect_proxies(args)
    if not proxies:
        print("没有可用的代理输入", file=sys.stderr)
        return 2

    tests = [t.strip() for t in args.test.split(",") if t.strip()]
    dl_nodes = get_download_nodes([n.strip() for n in args.nodes.split(",")]) if "download" in tests else None
    ul_nodes = get_upload_nodes([n.strip() for n in args.upload_nodes.split(",")]) if "upload" in tests else None
    dl_bytes = _parse_bytes(args.download_bytes)
    ul_bytes = _parse_bytes(args.upload_bytes)

    if not args.quiet:
        print(f"pspeed {__version__} | 代理 {len(proxies)} 个 | 维度 {','.join(tests)}"
              f" | 下载节点 {len(dl_nodes or [])} | 并发 {args.concurrency}", file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {
            ex.submit(probe_one, px, tests, dl_nodes, ul_nodes,
                      dl_bytes, ul_bytes, args.multi, args.timeout, args.tcp_timeout): px
            for px in proxies
        }
        for fut in as_completed(futs):
            px = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"proxy": px.raw, "protocol": px.scheme, "ok": False,
                       "tcp": None, "ip": None, "download": None, "upload": None,
                       "error": type(e).__name__}
            if not _passes_filter(res, args.min_speed, args.max_latency):
                res["_filtered"] = True
            results.append(res)
            if not args.quiet:
                tag = "OK " if res["ok"] else "FAIL"
                dl = res.get("download") or {}
                ip = (res.get("ip") or {}).get("out_ip") or "-"
                mb = dl.get("best_mbps") if dl.get("ok") else None
                mbs = f"{mb:.2f}Mbps" if mb is not None else "-"
                print(f"[{tag}] {px.display:<40} {mbs:<12} ip={ip}", file=sys.stderr)

    results.sort(key=lambda r: -((r.get("download") or {}).get("best_mbps") or 0))
    report = build_report(results)
    emit(report, pretty=args.pretty)
    return 0


if __name__ == "__main__":
    sys.exit(main())
