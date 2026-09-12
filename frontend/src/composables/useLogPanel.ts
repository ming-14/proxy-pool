/**
 * 日志面板集成：LogPanel 单例 + 后端日志源（增量轮询）。
 *
 * 数据流：后端 logbuffer 环形缓冲（2000 条）→ GET /api/logs?since=seq 增量
 * → 本地缓冲（与后端容量一致）→ LogPanel source 接口。
 *
 * 性能约定（核心）：
 * - 轮询只发生在面板可见期间：visibleChange=false 立即停表（零请求）；
 *   打开时启动并立即拉一次。面板关闭后不产生任何 /api/logs 请求。
 * - 增量拉取：本地只保留自上次游标起的新条目，响应体小。
 * - 本地缓冲有界（BUFFER_CAPACITY=2000，与后端一致），防内存膨胀。
 * - 拉取失败静默重试，不打断整体 UI（后端不可达时面板显示历史数据）。
 *
 * 主题：LogPanel 的 auto 模式读 body[data-theme]（本前端主题挂在 html 上），
 * 因此构造与运行时都显式跟随 themeStore，保证与面板主界面同主题。
 */
import { ref, watch } from 'vue';
import { api } from '@/api/client';
import { useThemeStore } from '@/stores/theme';
import { LogPanel } from '@/vendor/logpanel';
import type { LogEntry, LogsResponse } from '@/types/api';

export const POLL_INTERVAL = 1000;
export const BUFFER_CAPACITY = 2000;

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

/** 毫秒时间戳 → HH:MM:SS.mmm（与后端控制台日志时间格式对齐，日志面板专用） */
export function fmtLogTime(tsMs: number): string {
  const d = new Date(tsMs);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

// ---- 单例状态（SPA 常驻，应用生命周期内只建一次） ----
let panel: LogPanel | null = null;
let entries: LogEntry[] = [];
let cursor = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let listener: ((entry: LogEntry | { type: 'clear' }) => void) | null = null;
let polling = false;  // 防重入：慢响应时跳过本轮，避免请求堆积

/** 面板可见性（按钮高亮用；与 LogPanel visibleChange 同步） */
export const logPanelVisible = ref(false);

async function pollOnce(): Promise<void> {
  if (!listener || polling) return;
  polling = true;
  try {
    const data = await api.logs(cursor || undefined);
    if (needsFullReload(cursor, data)) {
      // 增量失效（serve 重启 seq 回绕 / 缓冲淘汰越过游标）：全量重拉并替换缓冲
      // 先拉取成功再清空 UI，拉取失败时保留旧数据（下轮重试）
      const full = await api.logs();
      cursor = full.cursor;
      entries = appendEntries([], full.entries);
      listener({ type: 'clear' });
      for (const e of full.entries) listener(e);
      return;
    }
    cursor = data.cursor;
    if (data.entries.length) {
      entries = appendEntries(entries, data.entries);
      for (const e of data.entries) listener(e);
    }
  } catch {
    // 静默：面板后端不可达时保留历史，下轮重试
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
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function getPanel(): LogPanel {
  if (!panel) {
    const theme = useThemeStore();
    panel = new LogPanel({
      source: {
        subscribe(cb) {
          listener = cb;
          return () => {
            listener = null;
            stopPoller();
          };
        },
        getEntries: () => entries,
        clear: () => {
          entries = [];
          api.clearLogs().catch(() => {});
          listener?.({ type: 'clear' });
        },
        getSize: () => entries.length,
        getCapacity: () => BUFFER_CAPACITY,
      },
      rules: {
        // 模块标签去掉 src. 前缀：src.use_cases.fetch_proxies → use_cases.fetch_proxies
        tag: (e) => e.tag.replace(/^src\./, ''),
        tsStr: (e) => fmtLogTime(e.ts),
      },
      theme: theme.theme,
      title: '控制台日志',
      storageKeyPrefix: 'proxy-pool-logpanel_',
      initialVisible: false,
    });
    // 性能核心：面板隐藏即停轮询（零 /api/logs 请求），打开即恢复
    panel.on('visibleChange', (visible) => {
      logPanelVisible.value = visible;
      if (visible) startPoller();
      else stopPoller();
    });
    // 主题跟随（构造时已用当前值，此后增量同步）
    watch(() => theme.theme, (t) => panel?.setOption('theme', t));
  }
  return panel;
}

export function useLogPanel() {
  return {
    visible: logPanelVisible,
    toggle() {
      const p = getPanel();
      if (p.isVisible()) p.hide();
      else p.show();
    },
  };
}