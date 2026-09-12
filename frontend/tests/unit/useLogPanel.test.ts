/**
 * useLogPanel 纯逻辑单测：增量合并裁剪 + 日志时间格式化。
 * 轮询/可见性节流属组件行为（依赖 DOM 定时器），由 Playwright E2E 覆盖。
 */
import { describe, expect, it } from 'vitest';
import { appendEntries, BUFFER_CAPACITY, fmtLogTime, needsFullReload } from '@/composables/useLogPanel';
import type { LogEntry, LogsResponse } from '@/types/api';

function entry(seq: number): LogEntry {
  return { seq, ts: 1, level: 1, levelName: 'INFO', tag: 'src.x', text: `m${seq}` };
}

describe('appendEntries', () => {
  it('空增量返回原数组引用', () => {
    const base = [entry(1)];
    expect(appendEntries(base, [])).toBe(base);
  });

  it('按序追加', () => {
    const merged = appendEntries([entry(1), entry(2)], [entry(3), entry(4)]);
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it('超容量裁剪保留最新', () => {
    const base = Array.from({ length: BUFFER_CAPACITY }, (_, i) => entry(i + 1));
    const merged = appendEntries(base, [entry(2001), entry(2002)]);
    expect(merged).toHaveLength(BUFFER_CAPACITY);
    expect(merged[0].seq).toBe(3);
    expect(merged.at(-1)?.seq).toBe(2002);
  });
});

describe('needsFullReload', () => {
  function resp(cursor: number, buffer_start: number): LogsResponse {
    return { entries: [], cursor, buffer_start };
  }

  it('正常增量不触发重拉', () => {
    expect(needsFullReload(5, resp(10, 1))).toBe(false);
  });

  it('serve 重启 seq 回绕触发重拉（新 cursor < 请求游标）', () => {
    expect(needsFullReload(1500, resp(3, 1))).toBe(true);
  });

  it('缓冲淘汰越过游标触发重拉（buffer_start > 请求游标）', () => {
    expect(needsFullReload(1500, resp(2000, 1900))).toBe(true);
  });

  it('首次拉取（cursor=0）恒不触发', () => {
    expect(needsFullReload(0, resp(0, 1))).toBe(false);
    expect(needsFullReload(0, resp(5, 1))).toBe(false);
  });
});

describe('fmtLogTime', () => {
  it('毫秒时间戳 → HH:MM:SS.mmm（两位补零，本地时区）', () => {
    // 用本地时间构造（fmtLogTime 走本地时区，避免 UTC/本地偏移差异）
    const ts = new Date(2026, 8, 12, 21, 44, 15, 324).getTime();
    expect(fmtLogTime(ts)).toBe('21:44:15.324');
  });

  it('毫秒补零到三位', () => {
    const ts = new Date(2026, 8, 12, 0, 0, 0, 5).getTime();
    expect(fmtLogTime(ts)).toBe('00:00:00.005');
  });
});