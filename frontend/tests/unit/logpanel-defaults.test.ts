/**
 * LogPanel 默认值与规则合并单测：默认访问器、宿主覆盖、文案替换与 tag 配色。
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_TAG_COLORS, DEFAULT_RULES } from '@/vendor/logpanel';
import type { LogPanelEntry } from '@/vendor/logpanel';
import { resolveRules, translator } from '@/vendor/logpanel/defaults';
import { resolveTagColor } from '@/vendor/logpanel/utils/tagColor';
import { createStorage } from '@/vendor/logpanel/utils/storage';

function entry(overrides: Partial<LogPanelEntry> = {}): LogPanelEntry {
  return { ts: new Date(2026, 8, 22, 10, 0, 0, 0).getTime(), level: 1, ...overrides };
}

describe('DEFAULT_RULES', () => {
  it('tsStr 缺省由 ts 格式化', () => {
    expect(DEFAULT_RULES.tsStr(entry())).toBe('10:00:00.000');
  });

  it('tsStr 显式给出时优先', () => {
    expect(DEFAULT_RULES.tsStr(entry({ tsStr: 'custom' }))).toBe('custom');
  });

  it('levelName 缺省按等级名补全', () => {
    expect(DEFAULT_RULES.levelName(entry({ level: 2 }))).toBe('WARN');
    expect(DEFAULT_RULES.levelName(entry({ level: 9 }))).toBe('?');
  });

  it('tag/text/stack 缺省容错', () => {
    expect(DEFAULT_RULES.tag(entry())).toBe('default');
    expect(DEFAULT_RULES.text(entry())).toBe('');
    expect(DEFAULT_RULES.stack(entry())).toBeNull();
  });

  it('isExpandable：带堆栈或 WARN 以上', () => {
    expect(DEFAULT_RULES.isExpandable(entry({ level: 1 }))).toBe(false);
    expect(DEFAULT_RULES.isExpandable(entry({ level: 2 }))).toBe(true);
    expect(DEFAULT_RULES.isExpandable(entry({ level: 1, stack: 'trace' }))).toBe(true);
  });
});

describe('resolveRules', () => {
  it('宿主覆盖生效，未覆盖项走默认', () => {
    const rules = resolveRules<LogPanelEntry>({ tag: () => 'custom' });
    expect(rules.tag(entry())).toBe('custom');
    expect(rules.text(entry({ text: 'kept' }))).toBe('kept');
  });

  it('tagColor 与内置表合并', () => {
    const rules = resolveRules<LogPanelEntry>({ tagColor: { scheduler: '#123456' } });
    expect(rules.tagColor.scheduler).toBe('#123456');
    expect(rules.tagColor.default).toBe(DEFAULT_TAG_COLORS.default);
  });
});

describe('translator', () => {
  it('缺省用内置文案并替换参数占位', () => {
    const t = translator();
    expect(t('logpanel.copied', { n: 3 })).toBe('已复制 3 条');
  });

  it('未知键原样返回', () => {
    expect(translator()('logpanel.unknown')).toBe('logpanel.unknown');
  });

  it('宿主传入的 t 优先', () => {
    expect(translator((key) => `[${key}]`)('logpanel.clear')).toBe('[logpanel.clear]');
  });
});

describe('resolveTagColor', () => {
  it('命中表内颜色', () => {
    expect(resolveTagColor('default', DEFAULT_TAG_COLORS)).toBe(DEFAULT_TAG_COLORS.default);
  });

  it('未命中按哈希出稳定颜色', () => {
    const first = resolveTagColor('use_cases.fetch_proxies', DEFAULT_TAG_COLORS);
    const second = resolveTagColor('use_cases.fetch_proxies', DEFAULT_TAG_COLORS);
    const other = resolveTagColor('framework.web.api', DEFAULT_TAG_COLORS);
    expect(first).toBe(second);
    expect(first).not.toBe(other);
    expect(first).toMatch(/^hsl\(\d+, 65%, 55%\)$/);
  });
});

describe('createStorage', () => {
  it('读写往返，键带前缀', () => {
    const storage = createStorage('test-logpanel_');
    storage.writeJSON('level_filter', [1, 2]);
    expect(storage.readJSON('level_filter', [])).toEqual([1, 2]);
    expect(localStorage.getItem('test-logpanel_level_filter')).toBe('[1,2]');
  });

  it('脏数据回退默认值', () => {
    localStorage.setItem('test-logpanel_geom', '{not json');
    const storage = createStorage('test-logpanel_');
    expect(storage.readJSON('geom', { x: 1 })).toEqual({ x: 1 });
  });

  it('布尔读写与缺省', () => {
    const storage = createStorage('test-logpanel_');
    expect(storage.readBool('autoscroll', true)).toBe(true);
    storage.writeBool('autoscroll', false);
    expect(storage.readBool('autoscroll', true)).toBe(false);
  });
});