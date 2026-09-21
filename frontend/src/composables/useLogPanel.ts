/**
 * 控制台日志接入：后端 logbuffer 环形缓冲（2000 条）→ GET /api/logs?since=seq 增量
 * → 本地缓冲（与后端容量一致）→ LogPanel 的 source / rules。
 *
 * 性能约定（核心）：
 * - 轮询只发生在面板可见期间：隐藏即停表（零请求），打开时启动并立即拉一次。
 * - 增量拉取：只取自上轮游标起的新条目，响应体小；本地缓冲有界，防内存膨胀。
 * - 拉取失败静默重试，不打断整体 UI（后端不可达时面板显示历史数据，下轮重试）。
 */
import { ref, watch } from 'vue';
import { api } from '@/api/client';
import type { LogPanelRules, LogPanelSource } from '@/vendor/logpanel';
import type { LogEntry, LogsResponse } from '@/types/api';

export const BUFFER_CAPACITY = 2000;
const POLL_INTERVAL = 1000;

/** 纯函数：合并增量并裁剪到容量上限（服务端 seq 单调递增，直接 concat 截尾）。可单测。 */
export function appendEntries(existing: LogEntry[], incoming: LogEntry[], max: number = BUFFER_CAPACITY): LogEntry[] {
  if (!incoming.length) return existing;
  const merged = existing.concat(incoming);
  return merged.length > max ? merged.slice(-max) : merged;
}

/**
 * 纯函数：增量是否已失效（请求游标不在服务端缓冲覆盖范围内）。
 * 触发场景：serve 重启后 seq 从 1 重新计数（新 cursor < 请求游标），
 * 或面板关闭期间缓冲淘汰越过游标（buffer_start > 请求游标）。
 * cursor=0 的首次拉取恒为全量，不判定失效。
 */
export function needsFullReload(cursor: number, data: LogsResponse): boolean {
  return cursor > 0 && (data.cursor < cursor || data.buffer_start > cursor);
}

// ---- 模块级状态（SPA 常驻，应用生命周期内只维护一份缓冲） ----
/** 面板可见性：侧边栏按钮高亮与轮询开关共用 */
const visible = ref(false);
let entries: LogEntry[] = [];
let cursor = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let polling = false; // 防重入：慢响应时跳过本轮，避免请求堆积
const listeners = new Set<(item: LogEntry | { type: 'clear' }) => void>();

function notify(item: LogEntry | { type: 'clear' }): void {
  for (const listener of listeners) listener(item);
}

async function pollOnce(): Promise<void> {
  if (polling) return;
  polling = true;
  try {
    const data = await api.logs(cursor || undefined);
    if (needsFullReload(cursor, data)) {
      // 增量失效（serve 重启 seq 回绕 / 缓冲淘汰越过游标）：全量重拉并整体替换
      const full = await api.logs();
      cursor = full.cursor;
      entries = appendEntries([], full.entries);
      notify({ type: 'clear' });
      for (const entry of full.entries) notify(entry);
      return;
    }
    cursor = data.cursor;
    if (data.entries.length) {
      entries = appendEntries(entries, data.entries);
      for (const entry of data.entries) notify(entry);
    }
  } catch {
    // 静默：保留历史数据，下一轮重试
  } finally {
    polling = false;
  }
}

function startPoller(): void {
  if (pollTimer) return;
  void pollOnce();
  pollTimer = setInterval(pollOnce, POLL_INTERVAL);
}

function stopPoller(): void {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

/** 面板数据源：清空时推送清空信号（组件据此丢弃展开态并重读快照） */
const source: LogPanelSource<LogEntry> = {
  subscribe(handler) {
    listeners.add(handler);
    return () => listeners.delete(handler);
  },
  getEntries: () => entries,
  clear() {
    entries = [];
    void api.clearLogs().catch(() => {});
    notify({ type: 'clear' });
  },
  getSize: () => entries.length,
  getCapacity: () => BUFFER_CAPACITY,
};

/** 取值覆盖项：模块标签去掉 src. 前缀；key 用服务端单调 seq（跨过滤与环形淘汰保持稳定） */
const rules: Partial<LogPanelRules<LogEntry>> = {
  tag: (entry) => entry.tag.replace(/^src\./, ''),
  key: (entry) => entry.seq,
};

/** 面板接入点：可见性（v-model）+ 数据源 + 取值规则 */
export function useLogPanel() {
  // 面板隐藏即停轮询（零 /api/logs 请求），打开即恢复
  watch(visible, (shown) => (shown ? startPoller() : stopPoller()), { immediate: true });
  return {
    visible,
    source,
    rules,
    toggle(): void {
      visible.value = !visible.value;
    },
  };
}