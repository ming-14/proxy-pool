"""代理池状态 SQLite 存储（domain.ports.ProxyStore 实现）

WAL 模式 + 组级增量 upsert：心跳时只写 dirty 组（~18 行 ~0.3ms），
vs 旧 JSON 全量序列化 706KB / 0.3s。复合主键 (proxy, group_name)
支持代理跨组副本（standby 成员复制进 trusted，每副本独立行）。

不兼容旧 .cache JSON 格式（直接切换，旧文件删除，无迁移逻辑）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from src.domain.proxy_pool import export_entry

logger = logging.getLogger(__name__)


# schema：proxies 复合主键支持跨组副本；group_meta 存组 ttl；source_paused 存源暂停；
# ippure_cache 存 IP 纯净度检查结果（按 (源名, IP) 分区，TTL 由用例层判断）。
# ip_purity_cache 为旧纯净度缓存表（键位与字段已变），启动即删，不做迁移。
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
DROP TABLE IF EXISTS ip_purity_cache;
CREATE TABLE IF NOT EXISTS ippure_cache (
    source    TEXT NOT NULL,
    ip        TEXT NOT NULL,
    result    TEXT NOT NULL,
    cached_at REAL NOT NULL,
    PRIMARY KEY (source, ip)
);
CREATE TABLE IF NOT EXISTS ip_geo_cache (
    ip           TEXT PRIMARY KEY,
    country      TEXT,
    country_code TEXT,
    province     TEXT,
    city         TEXT,
    lat          REAL,
    lng          REAL,
    cached_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
            # 条目形状与 export_state 共用同一规则（domain.export_entry）
            proxy_groups[g]["proxies"].append(export_entry(r["proxy"], dict(r)))

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

    # ------------------------------------------------------------------ IP 纯净度缓存
    def get_purity(self, source: str, ip: str) -> dict | None:
        """读取缓存；返回 {"result": {...}, "cached_at": float}，None 表示该源下无缓存"""
        row = self._conn.execute(
            "SELECT result, cached_at FROM ippure_cache WHERE source=? AND ip=?", (source, ip)
        ).fetchone()
        if row is None:
            return None
        return {"result": json.loads(row["result"]), "cached_at": row["cached_at"]}

    def save_purity(self, source: str, ip: str, result: dict) -> None:
        """写入/更新缓存（upsert by (source, ip)）"""
        self._conn.execute(
            "INSERT OR REPLACE INTO ippure_cache (source, ip, result, cached_at) VALUES (?, ?, ?, ?)",
            (source, ip, json.dumps(result, ensure_ascii=False), time.time()),
        )

    def close(self) -> None:
        """关闭连接（serve 退出时调用）"""
        self._conn.close()

    # ------------------------------------------------------------------ IP 归属缓存
    def get_geo_batch(self, ips: list[str]) -> dict[str, dict]:
        """批量读取归属缓存；返回 {ip: {country, country_code, province, city, lat, lng, cached_at}}

        只返回缓存中有记录的 IP（无记录不包含在返回 dict 中）。
        """
        if not ips:
            return {}
        placeholders = ",".join("?" for _ in ips)
        rows = self._conn.execute(
            f"SELECT ip, country, country_code, province, city, lat, lng, cached_at "
            f"FROM ip_geo_cache WHERE ip IN ({placeholders})", ips
        ).fetchall()
        result: dict[str, dict] = {}
        for r in rows:
            result[r["ip"]] = {
                "country": r["country"],
                "country_code": r["country_code"],
                "province": r["province"],
                "city": r["city"],
                "lat": r["lat"],
                "lng": r["lng"],
                "cached_at": r["cached_at"],
            }
        return result

    def save_geo_batch(self, results: dict[str, dict]) -> None:
        """批量写入/更新归属缓存（upsert by ip）

        results: {ip: {country, country_code, province, city, lat, lng}}
        """
        if not results:
            return
        now = time.time()
        rows = [
            (ip, d["country"], d["country_code"], d.get("province"), d.get("city"), d["lat"], d["lng"], now)
            for ip, d in results.items()
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO ip_geo_cache "
            "(ip, country, country_code, province, city, lat, lng, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    def get_geo_cache_fingerprint(self) -> str | None:
        """读 geo 缓存来源库指纹；未记录返回 None"""
        row = self._conn.execute("SELECT value FROM app_kv WHERE key='geo_db_fingerprint'").fetchone()
        return row["value"] if row else None

    def set_geo_cache_fingerprint(self, fingerprint: str) -> None:
        """记录 geo 缓存来源库指纹"""
        self._conn.execute(
            "INSERT OR REPLACE INTO app_kv (key, value) VALUES ('geo_db_fingerprint', ?)", (fingerprint,)
        )

    def clear_geo_cache(self) -> None:
        """清空归属缓存（切换 GeoLite2 库类型/版本时调用，防止旧粒度数据复用）"""
        self._conn.execute("DELETE FROM ip_geo_cache")
