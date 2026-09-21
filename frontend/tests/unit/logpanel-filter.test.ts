/**
 * LogPanel 过滤与搜索单测：匹配器编译与降级、命中分段、过滤判定、模块与等级统计。
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_RULES } from '@/vendor/logpanel';
import type { LogPanelEntry } from '@/vendor/logpanel';
import { accepts, collectTags, countLevels, createMatcher } from '@/vendor/logpanel/utils/filter';
import type { FilterState } from '@/vendor/logpanel/utils/filter';

const rules = { ...DEFAULT_RULES };

function entry(overrides: Partial<LogPanelEntry> = {}): LogPanelEntry {
  return { ts: 0, level: 1, levelName: 'INFO', tag: 'a', text: 'hello world', ...overrides };
}

function state(overrides: Partial<FilterState> = {}): FilterState {
  return {
    levels: new Set([0, 1, 2, 3]),
    hiddenTags: new Set<string>(),
    matcher: createMatcher('', false),
    ...overrides,
  };
}

describe('createMatcher', () => {
  it('空查询不生效且文本整体透传', () => {
    const matcher = createMatcher('', false);
    expect(matcher.active).toBe(false);
    expect(matcher.test('anything')).toBe(true);
    expect(matcher.segments('abc')).toEqual([{ text: 'abc', hit: false }]);
  });

  it('字面量模式忽略正则元字符', () => {
    const matcher = createMatcher('a.b', false);
    expect(matcher.test('xa.by')).toBe(true);
    expect(matcher.test('aXb')).toBe(false);
  });

  it('正则非法时标记 invalid 并降级为字面量匹配', () => {
    const matcher = createMatcher('([', true);
    expect(matcher.invalid).toBe(true);
    expect(matcher.test('x([y')).toBe(true);
  });

  it('命中分段：命中段标 hit，其余原样切分', () => {
    expect(createMatcher('wor', false).segments('hello world')).toEqual([
      { text: 'hello ', hit: false },
      { text: 'wor', hit: true },
      { text: 'ld', hit: false },
    ]);
  });

  it('同一匹配器可重复分段（内部游标复位）', () => {
    const matcher = createMatcher('o', false);
    expect(matcher.segments('foo')).toEqual(matcher.segments('foo'));
  });

  it('正则空匹配不吞掉后续内容', () => {
    expect(createMatcher('x*', true).segments('axb')).toEqual([
      { text: 'a', hit: false },
      { text: 'x', hit: true },
      { text: 'b', hit: false },
    ]);
  });
});

describe('accepts', () => {
  it('等级不在白名单即剔除', () => {
    expect(accepts(entry({ level: 0 }), rules, undefined, state({ levels: new Set([1, 2, 3]) }))).toBe(false);
  });

  it('模块在隐藏集即剔除', () => {
    expect(accepts(entry({ tag: 'a' }), rules, undefined, state({ hiddenTags: new Set(['a']) }))).toBe(false);
  });

  it('搜索不命中即剔除', () => {
    expect(accepts(entry({ text: 'hello' }), rules, undefined, state({ matcher: createMatcher('zzz', false) }))).toBe(false);
  });

  it('搜索走 formatText 钩子后的文本', () => {
    const hooks = { formatText: () => 'formatted-target' };
    expect(accepts(entry({ text: 'raw' }), rules, hooks, state({ matcher: createMatcher('target', false) }))).toBe(true);
  });
});

describe('collectTags', () => {
  it('去重并按字典序排序', () => {
    const entries = [entry({ tag: 'b' }), entry({ tag: 'a' }), entry({ tag: 'b' })];
    expect(collectTags(entries, rules)).toEqual(['a', 'b']);
  });
});

describe('countLevels', () => {
  it('按等级计数，越界等级忽略', () => {
    const entries = [entry({ level: 1 }), entry({ level: 3 }), entry({ level: 9 })];
    expect(countLevels(entries, rules)).toEqual([0, 1, 0, 1]);
  });
});