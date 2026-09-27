# 预取 FreeProxy.World 第 1 页 HTML：出口 IP 被站点 403 时（如 GitHub Actions），
# 改用 daily/base.db 里 freeproxy-world-https 组代理逐个尝试，成功即落盘；
# 之后把 FPW_FIRST_PAGE_FILE 指向该文件，freeproxy_world 源即跳过 Phase1 直连。
# 用法: python scripts/fetch_fpw_first_page.py --out .fpw-first-page.html
import argparse
import concurrent.futures
import os
import random
import sqlite3
import sys

sys.path.insert(0, ".")

import requests
import urllib3

from src.domain.models import FpwConfig
from src.interfaces.sources.freeproxy_world import BASE_URL, _build_headers, _validate_response
from src.interfaces.ua_generator import UAGenerator

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_DB = "daily/base.db"
PROXY_GROUP = "freeproxy-world-https"
PAGE_URL = f"{BASE_URL}?type=https&page=1"


def load_candidates(db_path: str) -> list:
    """从 base.db 读取候选代理，返回去掉 scheme 前缀的 ip:port 列表"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT proxy FROM proxies WHERE group_name = ?", (PROXY_GROUP,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0].split("://", 1)[-1] for r in rows]


def try_proxy(host: str, headers: dict, timeout: int):
    """用该 https 代理请求第 1 页；成功返回 HTML，任何失败返回 None"""
    try:
        resp = requests.get(PAGE_URL, headers=headers, timeout=timeout, verify=False,
                            proxies={"https": f"https://{host}"})
    except Exception:
        return None
    try:
        if resp.status_code == 200 and _validate_response(resp.text):
            return resp.text
        return None
    finally:
        resp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="用 daily/base.db 的代理预取 FreeProxy.World 第 1 页")
    parser.add_argument("--out", required=True, help="首页 HTML 落盘路径")
    parser.add_argument("--db", default=DEFAULT_DB, help="候选代理库路径")
    parser.add_argument("--limit", type=int, default=300, help="最多尝试的代理数")
    parser.add_argument("--workers", type=int, default=16, help="并发数")
    parser.add_argument("--timeout", type=int, default=6, help="单代理请求超时秒")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"候选代理库不存在: {args.db}", file=sys.stderr)
        return 1

    candidates = load_candidates(args.db)
    random.shuffle(candidates)  # 随机顺序：代理可用性差异大，避免总从同一批开始
    candidates = candidates[:args.limit]
    print(f"候选代理 {len(candidates)} 个 (组 {PROXY_GROUP}, 并发 {args.workers}, 超时 {args.timeout}s)")
    if not candidates:
        print(f"{args.db} 中无候选代理", file=sys.stderr)
        return 1

    headers = _build_headers(UAGenerator(), "local", FpwConfig())
    tried = 0
    for start in range(0, len(candidates), args.workers):
        batch = candidates[start:start + args.workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(try_proxy, host, headers, args.timeout) for host in batch]
            for future in concurrent.futures.as_completed(futures):
                tried += 1
                html = future.result()
                if html:
                    # newline="" 保留站点原始换行，避免 Windows 下文本模式改写为 CRLF
                    with open(args.out, "w", encoding="utf-8", newline="") as f:
                        f.write(html)
                    print(f"成功: 第 {tried} 个代理可用, 已写入 {args.out} ({len(html)} 字节)")
                    return 0
        print(f"已试 {tried}/{len(candidates)} 个代理, 暂无可用")

    print("全部候选代理均失败", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
