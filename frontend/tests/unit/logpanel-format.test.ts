/**
 * LogPanel 文本格式化单测：时间、纯文本行、导出文件名与钩子异常回退。
 */
import { describe, expect, it, vi } from 'vitest';
import { DEFAULT_RULES } from '@/vendor/logpanel';
import type { LogPanelEntry } from '@/vendor/logpanel';
import { entryText, exportFileName, formatEntryPlain, formatTime } from '@/vendor/logpanel/utils/format';

const rules = { ...DEFAULT_RULES };

function entry(overrides: Partial<LogPanelEntry> = {}): LogPanelEntry {
  return { ts: 0, level: 1, levelName: 'INFO', tag: 'a', text: 'message', ...overrides };
}

describe('formatTime', () => {
  it('毫秒时间戳 → HH:MM:SS.mmm（本地时区）', () => {
    expect(formatTime(new Date(2026, 8, 22, 21, 44, 15, 324).getTime())).toBe('21:44:15.324');
  });

  it('毫秒补零到三位', () => {
    expect(formatTime(new Date(2026, 8, 22, 0, 0, 0, 5).getTime())).toBe('00:00:00.005');
  });
});

describe('entryText', () => {
  it('无钩子时取 rules.text', () => {
    expect(entryText(entry(), rules)).toBe('message');
  });

  it('formatText 钩子可改写文本', () => {
    const hooks = { formatText: (text: string) => text.toUpperCase() };
    expect(entryText(entry(), rules, hooks)).toBe('MESSAGE');
  });

  it('钩子抛异常时回退原始文本并记录错误', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const hooks = {
      formatText: () => {
        throw new Error('boom');
      },
    };
    expect(entryText(entry(), rules, hooks)).toBe('message');
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe('formatEntryPlain', () => {
  it('时间 / 等级 / 模块 / 正文四段式', () => {
    const e = entry({ tsStr: '21:44:15.324', levelName: 'WARN', tag: 'scheduler', text: 'retry' });
    expect(formatEntryPlain(e, rules)).toBe('21:44:15.324 [WARN] [scheduler] retry');
  });
});

describe('exportFileName', () => {
  it('logs-YYYYMMDD-HHMMSS.txt', () => {
    expect(exportFileName(new Date(2026, 8, 22, 9, 5, 7))).toBe('logs-20260922-090507.txt');
  });
});