"""代理池缓存 (.cache.db) 只读查看器

独立于主工程业务逻辑的纯查看工具：直接读取 SQLite 缓存并展示
各组代理明细（加入时间、失败次数、过期倒计时）与源暂停状态。

- 数据来源：默认项目根 .cache.db，支持命令行参数覆盖路径
- 技术栈：tkinter + ttk（标准库，零额外依赖）
- 失效判定与领域模型保持一致：
    - ttl=None 永不过期；否则 now - joined_at >= ttl 视为已过期
    - consecutive_fails >= max_consecutive_fails（trusted 组用 trusted_max_consecutive_fails）视为超限失效
   （阈值来自 src.framework.config，避免硬编码；consecutive_fails 为连续失败计数，任何成功清零）
"""

from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from src.framework.config import (
    MAX_CONSECUTIVE_FAILS,
    PROXY_GROUPS,
    SOURCE_PAUSE_TTL,
    TRUSTED_GROUP,
    TRUSTED_MAX_CONSECUTIVE_FAILS,
)
from src.interfaces.sqlite_store import SqliteProxyStore

logger = logging.getLogger("proxy_pool.view")

# 即将过期的预警窗口（秒）
NEAR_EXPIRY_SECONDS = 60
# 自动刷新间隔（秒）
AUTO_REFRESH_INTERVAL = 5

# 行状态排序权重（展示顺序：已过期 > 超限 > 存活）
_STATUS_RANK = {"expired": 0, "invalid": 1, "alive": 2}


# ---------------------------------------------------------------- 纯数据逻辑（可单测）


def default_cache_path() -> str:
    """默认缓存路径：项目根（本文件所在目录）下的 .cache.db"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.db")


def load_cache(path: str) -> dict:
    """读取 SQLite 缓存；文件缺失或损坏返回空 dict（与 SqliteProxyStore 语义一致）"""
    if not os.path.exists(path):
        logger.warning("缓存文件不存在: %s", path)
        return {}
    try:
        store = SqliteProxyStore(path)
        data = store.load()
        store.close()
        return data
    except Exception as exc:
        logger.error("缓存文件解析失败 %s: %s", path, exc)
        return {}


def resolve_group_ttl(cache: dict, group_name: str) -> int | None:
    """解析组 TTL：优先缓存中的值，组不在缓存时回退配置 PROXY_GROUPS"""
    group = cache.get("proxy_groups", {}).get(group_name)
    ttl = group.get("ttl") if isinstance(group, dict) else None
    if ttl is not None:
        return ttl
    return PROXY_GROUPS.get(group_name)


def max_consecutive_fails_of(group_name: str) -> int:
    """组的连续失败上限：信任池组容错更高"""
    return TRUSTED_MAX_CONSECUTIVE_FAILS if group_name == TRUSTED_GROUP else MAX_CONSECUTIVE_FAILS


def build_proxy_rows(cache: dict, now: float | None = None) -> list[dict]:
    """从缓存构建代理明细行并按（状态优先级, 剩余时间）排序

    Row 字段: proxy/group/consecutive_fails/joined_at/ttl/remaining/status/near
    remaining: 剩余秒数；ttl=None 时为 None；负数表示已过期
    status:    "expired" | "invalid" | "alive"
    near:      存活且剩余不足 NEAR_EXPIRY_SECONDS（仅用于着色）
    """
    now = time.time() if now is None else now
    rows: list[dict] = []
    for group_name, group in cache.get("proxy_groups", {}).items():
        if not isinstance(group, dict):
            continue
        ttl = resolve_group_ttl(cache, group_name)
        max_consecutive_fails = max_consecutive_fails_of(group_name)
        for item in group.get("proxies", []):
            if not isinstance(item, dict):
                continue
            proxy = item.get("proxy")
            if not proxy:
                continue
            joined_at = item.get("joined_at", 0)
            consecutive_fails = item.get("consecutive_fails", 0)
            remaining = None if ttl is None else int(ttl - (now - joined_at))
            expired = remaining is not None and remaining <= 0
            invalid = consecutive_fails >= max_consecutive_fails
            status = "expired" if expired else ("invalid" if invalid else "alive")
            near = status == "alive" and remaining is not None and 0 < remaining <= NEAR_EXPIRY_SECONDS
            rows.append({
                "proxy": proxy,
                "group": group_name,
                "consecutive_fails": consecutive_fails,
                "joined_at": joined_at,
                "ttl": ttl,
                "remaining": remaining,
                "status": status,
                "near": near,
            })
    rows.sort(key=lambda r: (_STATUS_RANK[r["status"]], r["remaining"] if r["remaining"] is not None else 1 << 62))
    return rows


def build_group_summary(cache: dict, rows: list[dict], now: float | None = None) -> list[dict]:
    """按组汇总统计；组顺序 = 配置 PROXY_GROUPS 顺序 + 缓存中额外的组"""
    now = time.time() if now is None else now
    seen = set()
    summary: list[dict] = []
    for group_name in [*PROXY_GROUPS.keys(), *cache.get("proxy_groups", {}).keys()]:
        if group_name in seen:
            continue
        seen.add(group_name)
        group_rows = [r for r in rows if r["group"] == group_name]
        summary.append({
            "group": group_name,
            "ttl": resolve_group_ttl(cache, group_name),
            "total": len(group_rows),
            "expired": sum(1 for r in group_rows if r["status"] == "expired"),
            "invalid": sum(1 for r in group_rows if r["status"] == "invalid"),
            "alive": sum(1 for r in group_rows if r["status"] == "alive"),
        })
    return summary


def build_source_rows(cache: dict, now: float | None = None) -> list[dict]:
    """构建源暂停明细（含过期残留），按剩余时间升序

    Row 字段: source/paused_at/ttl/remaining/active
    """
    now = time.time() if now is None else now
    rows = []
    for source, val in cache.get("source_paused", {}).items():
        if not isinstance(val, dict):
            continue
        paused_at = val.get("paused_at", 0)
        ttl = val.get("ttl") or SOURCE_PAUSE_TTL
        remaining = int(paused_at + ttl - now)
        rows.append({
            "source": source,
            "paused_at": paused_at,
            "ttl": ttl,
            "remaining": remaining,
            "active": remaining > 0,
        })
    rows.sort(key=lambda r: r["remaining"])
    return rows


def format_ts(ts: int) -> str:
    """epoch 秒 -> 本地时间字符串"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def format_duration(seconds: int) -> str:
    """秒数 -> 紧凑可读时长（如 3分25秒 / 1时2分 / 2天3时）"""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天{hours}时"
    if hours:
        return f"{hours}时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def format_ttl(ttl: int | None) -> str:
    """TTL 展示：None -> 永不过期"""
    return "永不过期" if ttl is None else format_duration(ttl)


def format_remaining(row: dict) -> str:
    """剩余列展示：永不过期 / 已过期 / 剩 X"""
    if row["ttl"] is None:
        return "永不过期"
    if row["remaining"] <= 0:
        return "已过期"
    return f"剩 {format_duration(row['remaining'])}"


# ---------------------------------------------------------------- GUI


class CacheViewer:
    """.cache 只读查看器主窗口"""

    def __init__(self, root: tk.Tk, cache_path: str) -> None:
        self.root = root
        self.cache_path = cache_path
        self.cache: dict = {}
        self._auto_refresh_job: str | None = None

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------ UI 构建

    def _build_ui(self) -> None:
        self.root.title("Proxy Pool Cache 查看器")
        self.root.geometry("1080x640")
        self.root.minsize(760, 480)

        self._build_toolbar()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        # 标签页索引：总览 / 全部 / 每分组 / 源暂停
        self.overview_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.overview_tab, text="总览")
        self.all_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.all_tab, text="全部")
        self.group_tabs: dict[str, ttk.Frame] = {}
        self.source_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.source_tab, text="源暂停")

        self._build_overview()
        self._build_proxy_tree(self.all_tab)
        self.all_tree = self._tree_of(self.all_tab)
        self._build_source_tree()

        # 过滤输入框触发重建所有代理标签页
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._rebuild_proxy_tabs())
        self.filter_entry = ttk.Entry(self.toolbar, textvariable=self.filter_var, width=30)
        self.filter_entry.pack(side=tk.LEFT, padx=(12, 4))
        ttk.Label(self.toolbar, text="过滤代理关键字").pack(side=tk.LEFT)

    def _build_toolbar(self) -> None:
        self.toolbar = ttk.Frame(self.root)
        self.toolbar.pack(fill=tk.X, padx=6, pady=6)

        ttk.Button(self.toolbar, text="打开文件…", command=self._choose_file).pack(side=tk.LEFT)
        ttk.Button(self.toolbar, text="刷新", command=self.refresh).pack(side=tk.LEFT, padx=6)

        self.auto_refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.toolbar, text="自动刷新", variable=self.auto_refresh_var, command=self._toggle_auto_refresh
        ).pack(side=tk.LEFT, padx=6)

        # 文件信息标签右对齐
        self.file_info_var = tk.StringVar()
        ttk.Label(self.toolbar, textvariable=self.file_info_var).pack(side=tk.RIGHT)

    def _build_overview(self) -> None:
        """总览页：文件信息 + 分组统计表"""
        self.overview_info = ttk.Label(self.overview_tab, text="", justify=tk.LEFT)
        self.overview_info.pack(anchor=tk.W, padx=8, pady=(8, 4))

        columns = ("group", "ttl", "total", "alive", "invalid", "expired")
        self.overview_tree = self._make_tree(self.overview_tab, columns, {
            "group": "组", "ttl": "TTL", "total": "代理数",
            "alive": "存活", "invalid": "超限", "expired": "已过期",
        }, widths={"group": 200, "ttl": 110, "total": 70, "alive": 70, "invalid": 70, "expired": 80})

    def _build_proxy_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        columns = ("proxy", "group", "consecutive_fails", "joined", "remaining")
        tree = self._make_tree(parent, columns, {
            "proxy": "代理", "group": "组",             "consecutive_fails": "连续失败",
            "joined": "加入时间", "remaining": "剩余",
        }, widths={"proxy": 300, "group": 200, "consecutive_fails": 80, "joined": 150, "remaining": 120})
        tree.tag_configure("expired", foreground="#9e9e9e")
        tree.tag_configure("invalid", foreground="#c62828")
        tree.tag_configure("near", foreground="#ef6c00")
        return tree

    def _build_source_tree(self) -> None:
        columns = ("source", "paused_at", "ttl", "remaining")
        self.source_tree = self._make_tree(self.source_tab, columns, {
            "source": "代理源", "paused_at": "暂停时间", "ttl": "暂停时长", "remaining": "剩余",
        }, widths={"source": 380, "paused_at": 160, "ttl": 110, "remaining": 120})
        self.source_tree.tag_configure("expired", foreground="#9e9e9e")

    @staticmethod
    def _make_tree(parent: ttk.Frame, columns: tuple[str, ...], headings: dict[str, str],
                   widths: dict[str, int]) -> ttk.Treeview:
        """创建带纵向滚动条的 Treeview 表格"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], anchor=tk.W if col in ("proxy", "group", "source", "joined") else tk.E,
                        stretch=col in ("proxy", "group", "source"))
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)
        return tree

    @staticmethod
    def _tree_of(tab: ttk.Frame) -> ttk.Treeview:
        """取标签页内唯一的 Treeview（测试用）"""
        frame = next(w for w in tab.winfo_children() if isinstance(w, ttk.Frame))
        return next(w for w in frame.winfo_children() if isinstance(w, ttk.Treeview))

    # ------------------------------------------------------------ 数据渲染

    def _filtered_rows(self, rows: list[dict]) -> list[dict]:
        keyword = self.filter_var.get().strip().lower()
        if not keyword:
            return rows
        return [r for r in rows if keyword in r["proxy"].lower() or keyword in r["group"].lower()]

    def _fill_proxy_tree(self, tree: ttk.Treeview, rows: list[dict]) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for row in self._filtered_rows(rows):
            tag = "expired" if row["status"] == "expired" else ("invalid" if row["status"] == "invalid" else ("near" if row["near"] else ""))
            tree.insert("", tk.END, values=(
                row["proxy"], row["group"], row["consecutive_fails"],
                format_ts(row["joined_at"]), format_remaining(row),
            ), tags=(tag,) if tag else ())

    def _fill_overview(self, rows: list[dict], source_rows: list[dict]) -> None:
        mtime = os.path.getmtime(self.cache_path) if os.path.exists(self.cache_path) else 0
        info = (f"文件: {self.cache_path}\n"
                f"修改时间: {format_ts(int(mtime))}    "
                f"代理总数: {len(rows)}    "
                f"源暂停: {len(source_rows)} 个")
        self.overview_info.config(text=info)
        for item in self.overview_tree.get_children():
            self.overview_tree.delete(item)
        for row in build_group_summary(self.cache, rows):
            self.overview_tree.insert("", tk.END, values=(
                row["group"], format_ttl(row["ttl"]), row["total"],
                row["alive"], row["invalid"], row["expired"],
            ))

    def _fill_source_tree(self, source_rows: list[dict]) -> None:
        for item in self.source_tree.get_children():
            self.source_tree.delete(item)
        for row in source_rows:
            self.source_tree.insert("", tk.END, values=(
                row["source"], format_ts(row["paused_at"]),
                format_duration(row["ttl"]),
                format_duration(row["remaining"]) if row["active"] else "已解除（残留）",
            ), tags=("expired",) if not row["active"] else ())

    def _rebuild_proxy_tabs(self) -> None:
        """按当前过滤条件重建「全部」与各分组标签页"""
        rows = build_proxy_rows(self.cache)
        self._fill_proxy_tree(self.all_tree, rows)
        for group_name, tab in self.group_tabs.items():
            self._fill_proxy_tree(self._tree_of(tab), [r for r in rows if r["group"] == group_name])

    # ------------------------------------------------------------ 动作

    def refresh(self) -> None:
        """重新读取缓存文件并刷新全部视图"""
        self.cache = load_cache(self.cache_path)
        rows = build_proxy_rows(self.cache)
        source_rows = build_source_rows(self.cache)

        # 重建分组标签（组集合可能变化）
        for tab in self.group_tabs.values():
            self.notebook.forget(tab)
        self.group_tabs.clear()
        group_order = [*PROXY_GROUPS.keys(), *self.cache.get("proxy_groups", {}).keys()]
        seen = set()
        for group_name in group_order:
            if group_name in seen:
                continue
            seen.add(group_name)
            tab = ttk.Frame(self.notebook)
            self._build_proxy_tree(tab)
            self.notebook.add(tab, text=f"组:{group_name}")
            self.group_tabs[group_name] = tab

        self._fill_overview(rows, source_rows)
        self._rebuild_proxy_tabs()
        self._fill_source_tree(source_rows)
        self.file_info_var.set(f"{self.cache_path}（{time.strftime('%H:%M:%S')}）")
        logger.info("已刷新视图，代理 %d 条，源暂停 %d 个", len(rows), len(source_rows))

    def _choose_file(self) -> None:
        """文件选择对话框换缓存文件"""
        path = filedialog.askopenfilename(
            title="选择缓存文件", filetypes=[("SQLite 数据库", "*.db *.cache.db"), ("所有文件", "*.*")]
        )
        if path:
            self.cache_path = path
            self.refresh()

    def _toggle_auto_refresh(self) -> None:
        if self.auto_refresh_var.get():
            self._schedule_auto_refresh()
        elif self._auto_refresh_job is not None:
            self.root.after_cancel(self._auto_refresh_job)
            self._auto_refresh_job = None

    def _schedule_auto_refresh(self) -> None:
        if not self.auto_refresh_var.get():
            return
        self.refresh()
        self._auto_refresh_job = self.root.after(AUTO_REFRESH_INTERVAL * 1000, self._schedule_auto_refresh)


def main() -> int:
    """入口：python view.py [缓存路径]"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cache_path = sys.argv[1] if len(sys.argv) > 1 else default_cache_path()
    root = tk.Tk()
    viewer = CacheViewer(root, cache_path)
    if not viewer.cache:
        messagebox.showwarning("缓存为空", f"缓存文件不存在或解析失败:\n{cache_path}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
