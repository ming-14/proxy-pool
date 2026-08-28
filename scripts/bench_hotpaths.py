# 性能基准：加载指定 .cache.db 数据，量化热路径耗时与内存
# 用法: python scripts/bench_hotpaths.py [db_path]（缺省用项目根 .cache.db）
import sys
import time

sys.path.insert(0, ".")

from src.domain.proxy_pool import ProxyPool
from src.framework import config
from src.interfaces.sqlite_store import SqliteProxyStore

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else config.CACHE_DB

# 加载与原服务相同的数据
store = SqliteProxyStore(DB_PATH)
state = store.load()
print("加载状态: 组数=%d" % len(state.get("proxy_groups", {})))
for name, g in state.get("proxy_groups", {}).items():
    print("  %-28s %6d 条" % (name, len(g["proxies"])))
store.close()

pool = ProxyPool(
    config.PROXY_GROUPS,
    max_consecutive_fails=config.MAX_CONSECUTIVE_FAILS,
    trusted_max_consecutive_fails=config.TRUSTED_MAX_CONSECUTIVE_FAILS,
    default_pause_ttl=config.SOURCE_PAUSE_TTL,
    requestable_groups=config.REQUESTABLE_GROUPS,
    trusted_group=config.TRUSTED_GROUP,
    blacklist_group=config.BLACKLIST_GROUP,
)
pool.restore_state(state)
del state

N = 200

def bench(name, fn, n=N):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = (time.perf_counter() - t0) / n * 1000
    print(f"{name:40s} {dt:8.3f} ms/次")
    return dt

print("\n--- 热路径基准 ---")
bench("get_available_proxies()", lambda: pool.get_available_proxies())
bench("get_active_proxies(每个组)", lambda: [pool.get_active_proxies(g) for g in pool.proxy_groups])
bench("export_state() 全量序列化", lambda: pool.export_state())
# dirty 组导出（模拟 30s 增量 save：standby + trusted）
for g in ("Proxy-pool-standby-queue", "trusted"):
    pool._dirty_groups.add(g)
bench("export_group(standby+trusted) 增量", lambda: [pool.export_group(g) for g in ("Proxy-pool-standby-queue", "trusted")])

# 内存基准：export_state 全量序列化的临时分配（tracemalloc 前后快照差值）
import tracemalloc
print("\n--- 内存基准 ---")
tracemalloc.start()
snap1 = tracemalloc.take_snapshot()
_ = pool.export_state()  # 触发完整结构
snap2 = tracemalloc.take_snapshot()
tracemalloc.stop()
total = sum(s.size_diff for s in snap2.compare_to(snap1, "filename"))
print(f"export_state 相关分配: {total/1024:.0f} KB")
