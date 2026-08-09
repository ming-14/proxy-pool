"""代理池状态 SQLite 存储（domain.ports.ProxyStore 实现）

WAL 模式 + 组级增量 upsert：心跳时只写 dirty 组（~18 行 ~0.3ms），
vs 旧 JSON 全量序列化 706KB / 0.3s。复合主键 (proxy, group_name)
支持代理跨组副本（standby 成员复制进 trusted，每副本独立行）。

不兼容旧 .cache JSON 格式（直接切换，旧文件删除，无迁移逻辑）。
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)


# 三表 schema：proxies 复合主键支持跨组副本；group_meta 存组 ttl；source_paused 存源暂停
_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    proxy             TEXT NOT NULL,
    group_name        TEXT NOT NULL,
    joined_at         INTEGER NOT NULL,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    latency           REAL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    pinned_at         INTEGER,
    source_group      TEXT,
    PRIMARY KEY (proxy, group_name)
);
CREATE TABLE IF NOT EXISTS source_paused (
    source    TEXT PRIMARY KEY,
    paused_at INTEGER NOT NULL,
    ttl       INTEGER
);
CREATE TABLE IF NOT EXISTS group_meta (
    group_name TEXT PRIMARY KEY,
    ttl        INTEGER
);
"""

# 代理行 INSERT 语句（8 列）
_INSERT_PROXY = (
    "INSERT INTO proxies "
    "(proxy, group_name, joined_at, consecutive_fails, latency, pinned, pinned_at, source_group) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


class SqliteProxyStore:
    """SQLite 持久化（WAL 模式，组级增量 upsert）

    单连接，serve 主循环 + Web 面板同事件循环同线程访问，无并发竞争。
    WAL 模式下若有外部进程读 .cache.db，读不阻塞写。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # isolation_level=None → autocommit，事务用显式 BEGIN/COMMIT 控制
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def load(self) -> dict:
        """加载全量状态（启动时调用，返回 export_state 兼容格式）

        数据库文件损坏时捕获异常返回空 dict（空池启动，重新拉取）。
        返回格式: {"proxy_groups": {name: {"ttl": ..., "proxies": [...]}}, "source_paused": {...}}
        """
        try:
            return self._load_impl()
        except sqlite3.DatabaseError as e:
            logger.warning("数据库损坏将使用空池: %s (%s)", self._db_path, e)
            return {}

    def _load_impl(self) -> dict:
        # 组元数据（ttl）
        meta_rows = self._conn.execute("SELECT group_name, ttl FROM group_meta").fetchall()
        ttl_map = {r["group_name"]: r["ttl"] for r in meta_rows}

        # 代理按 group_name 分组
        proxy_rows = self._conn.execute(
            "SELECT proxy, group_name, joined_at, consecutive_fails, latency, pinned, pinned_at, source_group FROM proxies"
        ).fetchall()

        proxy_groups: dict[str, dict] = {}
        for r in proxy_rows:
            g = r["group_name"]
            if g not in proxy_groups:
                proxy_groups[g] = {"ttl": ttl_map.get(g), "proxies": []}
            entry: dict = {
                "proxy": r["proxy"],
                "joined_at": r["joined_at"],
                "consecutive_fails": r["consecutive_fails"],
            }
            # 与 export_state 对齐：latency/pinned/source_group 仅在非默认值时导出
            if r["latency"] is not None:
                entry["latency"] = r["latency"]
            if r["pinned"]:
                entry["pinned"] = True
                entry["pinned_at"] = r["pinned_at"]
            if r["source_group"] is not None:
                entry["source_group"] = r["source_group"]
            proxy_groups[g]["proxies"].append(entry)

        # 补全空组（有 meta 无代理的组也要返回，restore_state 需要组骨架）
        for g, ttl in ttl_map.items():
            if g not in proxy_groups:
                proxy_groups[g] = {"ttl": ttl, "proxies": []}

        # 源暂停
        sp_rows = self._conn.execute("SELECT source, paused_at, ttl FROM source_paused").fetchall()
        source_paused = {r["source"]: {"paused_at": r["paused_at"], "ttl": r["ttl"]} for r in sp_rows}

        # 空库返回 {}（与 restore_state 兼容，等价于文件不存在的空池启动）
        if not proxy_groups and not source_paused:
            return {}
        return {"proxy_groups": proxy_groups, "source_paused": source_paused}

    def save_full(self, state: dict) -> None:
        """全量替换（CLI 命令退出 / serve 退出，确保一致性）

        事务内 DELETE 三表 + INSERT 全部。
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM proxies")
            self._conn.execute("DELETE FROM source_paused")
            self._conn.execute("DELETE FROM group_meta")
            self._insert_groups(state.get("proxy_groups", {}))
            self._insert_source_paused(state.get("source_paused", {}))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def save_group(self, group_name: str, group_data: dict) -> None:
        """增量保存单个组（事务内 DELETE 该组旧行 + INSERT 新行）

        Args:
            group_data: {"ttl": ..., "proxies": [{"proxy", "joined_at", ...}]}
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM proxies WHERE group_name=?", (group_name,))
            self._conn.execute("DELETE FROM group_meta WHERE group_name=?", (group_name,))
            self._insert_one_group(group_name, group_data)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def save_source_paused(self, source_paused: dict) -> None:
        """增量保存源暂停状态（全量替换 source_paused 表）"""
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM source_paused")
            self._insert_source_paused(source_paused)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _insert_groups(self, proxy_groups: dict) -> None:
        """批量插入所有组（proxies + group_meta）"""
        meta_rows = []
        proxy_rows = []
        for name, group_data in proxy_groups.items():
            meta_rows.append((name, group_data.get("ttl")))
            for p in group_data.get("proxies", []):
                proxy_rows.append(self._proxy_row(name, p))
        if meta_rows:
            self._conn.executemany(
                "INSERT INTO group_meta (group_name, ttl) VALUES (?, ?)", meta_rows
            )
        if proxy_rows:
            self._conn.executemany(_INSERT_PROXY, proxy_rows)

    def _insert_one_group(self, group_name: str, group_data: dict) -> None:
        """插入单个组（group_meta + proxies）"""
        self._conn.execute(
            "INSERT INTO group_meta (group_name, ttl) VALUES (?, ?)",
            (group_name, group_data.get("ttl")),
        )
        rows = [self._proxy_row(group_name, p) for p in group_data.get("proxies", [])]
        if rows:
            self._conn.executemany(_INSERT_PROXY, rows)

    def _insert_source_paused(self, source_paused: dict) -> None:
        """批量插入源暂停状态"""
        rows = [(src, val["paused_at"], val.get("ttl")) for src, val in source_paused.items()]
        if rows:
            self._conn.executemany(
                "INSERT INTO source_paused (source, paused_at, ttl) VALUES (?, ?, ?)", rows
            )

    @staticmethod
    def _proxy_row(group_name: str, p: dict) -> tuple:
        """从导出的代理 dict 构造 SQL 行元组"""
        return (
            p["proxy"], group_name, p["joined_at"], p.get("consecutive_fails", 0),
            p.get("latency"), bool(p.get("pinned", False)), p.get("pinned_at"),
            p.get("source_group"),
        )

    def close(self) -> None:
        """关闭连接（serve 退出时调用）"""
        self._conn.close()
