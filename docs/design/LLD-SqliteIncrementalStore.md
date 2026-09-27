# LLD：SQLite 增量持久化

## 1. 模块概述

### 1.1 背景

`serve` 模式下代理池状态持久化（`Container.save()`）当前采用全量 JSON 序列化写入 `.cache`：
每次 save 调用 `ProxyPool.export_state()` 遍历全部 ~8000 个代理构建 dict，再 `json.dump`
序列化 ~706KB 写文件，耗时 ~0.3s。

save 触发点密集：
- serve 主循环每轮心跳的 `fill_if_needed`（30s 节流）
- **补测任务完成时立即 save（`sync_and_save_now`，不节流）**
- Web 控制面板每个写动作（fetch/blacklist/trust/pin/lock/settings 等）
- CLI 命令退出时

补测任务密集时 30s 内可触发 7 次 save，累计 ~2.3s 阻塞事件循环，影响心跳/请求响应。

### 1.2 痛点量化

| 组 | 代理数 | 序列化体积 | 心跳时是否变化 |
|---|---|---|---|
| freeproxy-world-http/https/socks5 | 7657 | 669KB (95%) | 否（仅拉取时变） |
| Proxy-pool-standby-queue | 15 | 1KB | 是 |
| trusted | 3 | <1KB | 是 |
| blacklist | 261 | 33KB | 拉黑时变 |
| **全量** | 7936 | **706KB / 0.3s** | — |

**95% 的序列化开销浪费在心跳时不变的大组上**。心跳仅修改 standby/trusted（~18 行），
却每次全量序列化 706KB。

### 1.3 目标

用 SQLite 替换 JSON 文件存储，实现**组级增量 upsert**：
- save 只写 dirty 组（心跳时 18 行 ~0.3ms，vs 全量 0.3s）
- 不兼容旧 `.cache` 格式（直接切换，旧文件删除）
- 保持干净架构：store 在 interfaces 层，dirty tracking 在 domain 层

### 1.4 涉及模块

```
src/domain/proxy_pool.py        — dirty group tracking（domain 层，纯内存）
src/domain/ports.py             — ProxyStore 端口扩展为增量接口
src/interfaces/sqlite_store.py  — 新增 SqliteProxyStore（interfaces 层）
src/interfaces/json_store.py    — 删除（不兼容旧格式）
src/framework/container.py      — 切换 store + save() 改增量
src/framework/serve.py          — 退出改 save_full()
src/framework/cli.py            — CLI 退出改 save_full()
src/framework/config.py         — cache_db 路径配置
```

## 2. 详细设计目标

| 目标 | 指标 |
|---|---|
| 心跳成功后 save 耗时 | < 1ms（现状 0.3s） |
| 拉黑后 save 耗时 | < 10ms（现状 0.3s） |
| 退出全量 save_full 耗时 | < 0.1s（现状 0.3s，SQLite 批量 upsert 比 JSON 快） |
| 事件循环阻塞 | save 不阻塞（<1ms 可忽略） |
| 数据耐久性 | WAL 模式，进程崩溃不损坏（SQLite 事务保证） |
| 并发 | WAL 允许读不阻塞写（Web 面板读状态不阻塞 save） |

## 3. 数据模型 / 数据库设计

### 3.1 SQLite 表结构

```sql
-- 代理主表：复合主键支持代理跨组副本（standby 成员复制进 trusted）
CREATE TABLE IF NOT EXISTS proxies (
    proxy             TEXT NOT NULL,
    group_name        TEXT NOT NULL,
    joined_at         REAL NOT NULL,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    latency           REAL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    pinned_at         REAL,
    source_group      TEXT,
    PRIMARY KEY (proxy, group_name)
);

-- 源暂停状态
CREATE TABLE IF NOT EXISTS source_paused (
    source    TEXT PRIMARY KEY,
    paused_at REAL NOT NULL,
    ttl       INTEGER
);

-- 组元数据（ttl，用于 load 时恢复组结构）
CREATE TABLE IF NOT EXISTS group_meta (
    group_name TEXT PRIMARY KEY,
    ttl        REAL
);
```

### 3.2 PRAGMA 配置

```python
PRAGMA journal_mode=WAL        -- Write-Ahead Logging，读不阻塞写
PRAGMA synchronous=NORMAL      -- 平衡耐久性与性能（WAL 下崩溃不损坏）
PRAGMA foreign_keys=ON
```

### 3.3 索引

复合主键 `(proxy, group_name)` 自带索引。`load()` 按 group_name 分组查询，主键第二列已索引，
无需额外索引。

### 3.4 文件位置

`config/config.toml` 的 `[paths]`：
```toml
cache_db = ".cache.db"   # SQLite 数据库文件（相对项目根）
```

## 4. 核心类 / 接口定义

### 4.1 ProxyStore 端口（domain/ports.py）

```python
class ProxyStore(Protocol):
    """代理池状态存储端口：支持增量与全量两种持久化模式"""

    def load(self) -> dict:
        """加载全量状态（启动时调用，返回 export_state 兼容格式）"""
        ...

    def save_full(self, state: dict) -> None:
        """全量替换（CLI 命令退出 / serve 退出，确保一致性）"""
        ...

    def save_group(self, group_name: str, group_data: dict) -> None:
        """增量保存单个组（事务内 DELETE 该组旧行 + INSERT 新行）

        Args:
            group_data: {"ttl": ..., "proxies": [{"proxy", "joined_at", ...}]}
        """
        ...

    def save_source_paused(self, source_paused: dict) -> None:
        """增量保存源暂停状态（全量替换 source_paused 表）"""
        ...
```

### 4.2 SqliteProxyStore（interfaces/sqlite_store.py）

```python
class SqliteProxyStore:
    """SQLite 持久化（WAL 模式，组级增量 upsert）"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def load(self) -> dict:
        """SELECT 全表 → 按 group_name 组装 {proxy_groups, source_paused}"""
        ...

    def save_full(self, state: dict) -> None:
        """事务内 TRUNCATE + INSERT 全部（退出用）"""
        ...

    def save_group(self, group_name: str, group_data: dict) -> None:
        """事务内 DELETE WHERE group_name=? + INSERT OR REPLACE 该组所有代理"""
        ...

    def save_source_paused(self, source_paused: dict) -> None:
        """事务内 DELETE + INSERT source_paused"""
        ...

    def close(self) -> None:
        """关闭连接（serve 退出时调用）"""
        self._conn.close()
```

### 4.3 ProxyPool dirty tracking（domain/proxy_pool.py）

```python
class ProxyPool:
    def __init__(self, ...):
        ...
        self._dirty_groups: set[str] = set()
        self._source_paused_dirty: bool = False

    # 修改方法末尾标记 dirty（8 个方法）：
    def add_to_group(self, group_name, ...):
        ...
        self._dirty_groups.add(group_name)

    def move_proxy(self, proxy, to_group, ...):
        ...
        if src_group is not None:
            self._dirty_groups.add(src_group)
        self._dirty_groups.add(to_group)

    def pin(self, proxy): ...        # → self._dirty_groups.add(group)
    def unpin(self, proxy): ...      # → self._dirty_groups.add(group)
    def update_latency(self, ...): ...  # → self._dirty_groups.add(group)
    def remove_all_copies(self, ...): ...  # → 标记所有涉及的组
    def reset_consecutive_fails(self, ...): ...  # → self._dirty_groups.add(group)
    def pause_source(self, ...): ...  # → self._source_paused_dirty = True

    def _cleanup_all_expired(self):
        ...  # 清理过期代理时，标记被清理的组 dirty

    # 查询 / 清理 dirty（供 Container.save 调用）：
    def dirty_groups(self) -> set[str]:
        return set(self._dirty_groups)

    def source_paused_dirty(self) -> bool:
        return self._source_paused_dirty

    def clear_dirty(self) -> None:
        self._dirty_groups.clear()
        self._source_paused_dirty = False

    # 单组导出（供 save_group 用）：
    def export_group(self, group_name: str) -> dict:
        """导出单个组 {ttl, proxies: [...]}（含过期清理）"""
        ...
```

### 4.4 Container（framework/container.py）

```python
@dataclass
class Container:
    ...
    store: SqliteProxyStore

    def save(self) -> None:
        """增量保存：只写 dirty 组（serve 主循环 / Web 控制动作高频调用）"""
        dirty = self.pool.dirty_groups()
        if dirty:
            for g in dirty:
                self.store.save_group(g, self.pool.export_group(g))
        if self.pool.source_paused_dirty():
            self.store.save_source_paused(self.pool.export_source_paused())
        self.pool.clear_dirty()

    def save_full(self) -> None:
        """全量保存（CLI 退出 / serve 退出，确保一致性）"""
        self.store.save_full(self.pool.export_state())
        self.pool.clear_dirty()
```

## 5. 详细流程

### 5.1 save 增量流程（serve 主循环 / Web 控制动作）

```
调用方调 c.save()
    │
    ▼
dirty = pool.dirty_groups()          # 读取 dirty 组集合
    │
    ├─ dirty 为空 → 直接返回（无变更，零开销）
    │
    └─ dirty 非空：
        for g in dirty:
            group_data = pool.export_group(g)    # 单组导出（仅该组代理）
            store.save_group(g, group_data)       # SQLite 事务：DELETE + INSERT
        pool.clear_dirty()                        # 清 dirty 标记
```

**心跳成功场景**：`update_latency` / `reset_consecutive_fails` 标记 standby + trusted dirty
（2 组 ~18 行）。save 执行 2 个 save_group 事务，~0.3ms。

**拉黑场景**：`remove_all_copies` 标记源组 + blacklist dirty。save 执行 2 个 save_group，
blacklist 261 行 ~5ms。

### 5.2 save_full 流程（CLI / serve 退出）

```
c.save_full()
    │
    ▼
state = pool.export_state()          # 全量导出（所有组）
store.save_full(state)               # SQLite 事务：TRUNCATE + INSERT 全部
pool.clear_dirty()
```

### 5.3 load 流程（启动）

```
build_container()
    │
    ▼
store = SqliteProxyStore(config.CACHE_DB)
state = store.load()                 # SELECT * FROM proxies → 按 group_name 分组
pool.restore_state(state)            # 恢复内存状态
```

`load()` 返回格式与原 `export_state()` 输出兼容：
```python
{
    "proxy_groups": {
        "trusted": {"ttl": None, "proxies": [{"proxy": "...", "joined_at": ..., ...}]}
    },
    "source_paused": {"src.interfaces.sources.ip89": {"paused_at": ..., "ttl": 1200}}
}
```

### 5.4 dirty tracking 标记点

| ProxyPool 方法 | 触发场景 | 标记的 dirty 组 |
|---|---|---|
| `add_to_group` | 拉取入组 / fill 补入 / 拉黑 | 该组 |
| `move_proxy` | fill 替换 / 晋级 trusted / 锁定 | 源组 + 目标组 |
| `pin` / `unpin` | Web 面板固定/解固 | 代理所在组 |
| `update_latency` | 心跳成功 | 代理所在组 |
| `remove_all_copies` | 拉黑清理副本 | 所有含该代理的组 + blacklist |
| `reset_consecutive_fails` | 请求/心跳/补测成功 | 代理所在组 |
| `pause_source` | 源暂停 / Web 面板 | `_source_paused_dirty=True` |
| `_cleanup_all_expired` | 过期清理（被动触发） | 清理了代理的组 |

## 6. 错误处理与边界情况

### 6.1 SQLite 异常

| 场景 | 处理 |
|---|---|
| 数据库文件损坏 | `load()` 捕获异常，告警并返回空 dict（空池启动，重新拉取） |
| 写入失败（磁盘满/权限） | `save_group` 抛异常，serve 主循环 catch 记日志，不中断（下轮重试） |
| 连接关闭后调用 | 抛 `RuntimeError`（编程错误，应避免） |

### 6.2 dirty 丢失

进程崩溃时未 flush 的 dirty 标记丢失，内存变更未落盘。**可接受**：
代理状态（consecutive_fails/latency）丢失不致命，重新探测即可恢复。
退出时 `save_full()` 确保一致。

### 6.3 跨组副本一致性

代理可跨组存在副本（standby 成员复制进 trusted）。`remove_all_copies` 单次遍历所有组
清理副本，标记所有涉及组 dirty。SQLite 复合主键 `(proxy, group_name)` 保证每副本独立行，
`save_group` 按组删除+插入，不会残留跨组脏行。

### 6.4 WAL 检查点

WAL 模式下 `-wal` 文件增长，SQLite 在合适时机自动 checkpoint。无需手动管理。
若 `.cache.db-wal` 过大（异常退出残留），下次启动 SQLite 自动恢复并 checkpoint。

### 6.5 并发

`SqliteProxyStore` 单连接，serve 主循环 + Web 面板同事件循环同线程访问，无并发竞争。
WAL 模式下若有外部进程读 `.cache.db`，读不阻塞写。

## 7. 性能与资源估算

### 7.1 save 耗时对比

| 场景 | 现状（JSON 全量） | SQLite 增量 | 提升 |
|---|---|---|---|
| 心跳成功（2 组 18 行） | 0.3s / 706KB | ~0.3ms | **1000×** |
| 拉黑（3 组 279 行） | 0.3s / 706KB | ~5ms | **60×** |
| 补测完成（1-2 组） | 0.3s / 706KB | ~0.3ms | **1000×** |
| 退出 save_full（全量） | 0.3s / 706KB | ~0.08s（批量 upsert） | **4×** |

### 7.2 内存占用

SQLite 连接 ~100KB 常驻。`load()` 一次性读全表构建 dict（与原 `json.load` 同量级）。
`export_group` 单组导出只构建该组 dict，不遍历大组。

### 7.3 磁盘

`.cache.db` ~500-700KB（与原 `.cache` 同级）。WAL 文件 `.cache.db-wal` 运行时存在，
退出后 checkpoint 合并回主文件。

## 8. 测试要点

### 8.1 SqliteProxyStore 单测

| 测试 | 验证 |
|---|---|
| `test_load_empty` | 新建 db，load 返回空 dict |
| `test_save_full_load_roundtrip` | save_full 后 load 还原 |
| `test_save_group_incremental` | save_group 只更新该组，其他组不变 |
| `test_save_group_overwrite` | 同组二次 save_group 替换旧行 |
| `test_save_source_paused` | source_paused 增量 |
| `test_proxy_cross_group` | 代理跨组副本独立存储 |
| `test_wal_not_corrupt_on_crash` | 模拟异常退出后 load 不损坏 |

### 8.2 ProxyPool dirty tracking 单测

| 测试 | 验证 |
|---|---|
| `test_move_proxy_marks_dirty` | move_proxy 标记源组+目标组 |
| `test_update_latency_marks_dirty` | update_latency 标记组 |
| `test_clear_dirty` | clear_dirty 后 dirty_groups 为空 |
| `test_export_group` | export_group 只含该组代理 |
| `test_cleanup_marks_dirty` | 过期清理标记被清理的组 |

### 8.3 Container 集成

| 测试 | 验证 |
|---|---|
| `test_save_skips_when_no_dirty` | 无 dirty 时 save 零 IO |
| `test_save_only_dirty_groups` | save 只写 dirty 组（用 spy store 验证） |
| `test_save_full_writes_all` | save_full 全量写 |

### 8.4 e2e

`tests/e2e/` 现有 serve 全链路测试适配 SQLite store，验证：
- 启动 load → serve 运行 → 退出 save_full → 重启 load 状态一致
- 心跳后 .cache.db 只有 dirty 组行被更新

### 8.5 性能验证

`python cli.py serve --type https` 120s + cProfile：
- save 耗时从 0.3s 降到 <5ms
- save 退出 cProfile top 热点
- 内存/CPU 稳定

## 9. 实施步骤

1. 新建 `src/interfaces/sqlite_store.py`（SqliteProxyStore）
2. 改 `src/domain/ports.py`（ProxyStore 端口扩展）
3. 改 `src/domain/proxy_pool.py`（dirty tracking + export_group）
4. 删 `src/interfaces/json_store.py`
5. 改 `src/framework/container.py`（切换 store + save 增量）
6. 改 `src/framework/serve.py`（退出 save_full）
7. 改 `src/framework/cli.py`（CLI 退出 save_full）
8. 改 `src/framework/config.py`（cache_db 路径）
9. 新建 `tests/interfaces/test_sqlite_store.py`
10. 更新 `tests/domain/test_proxy_pool.py`（dirty tracking）
11. 更新 e2e 测试
12. 删除 `.cache`（旧 JSON 文件）
13. 跑 `pytest` 全量通过
14. 跑 `python cli.py serve --type https` 验证性能
15. 同步更新 `README.md` / `docs/architecture.md` / `docs/filestree.md`
