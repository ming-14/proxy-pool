/**
 * 过滤与搜索：等级白名单 + 隐藏模块集 + 搜索匹配器。
 *
 * 匹配器在创建时编译一次正则（逐条重建正则是过滤的主要开销），正则非法时
 * 与宿主 UI 的标红一致地降级为字面量匹配。
 */
import type { HighlightSegment, LogPanelHooks, LogPanelRules } from '../types';
import { entryText } from './format';

export interface Matcher {
  /** 非空查询才生效 */
  active: boolean;
  /** 正则模式但表达式非法（UI 据此标红输入框） */
  invalid: boolean;
  test(text: string): boolean;
  /** 命中分段（含未命中段），供高亮渲染 */
  segments(text: string): HighlightSegment[];
}

/** 过滤条件：等级白名单、隐藏模块集、搜索匹配器 */
export interface FilterState {
  levels: ReadonlySet<number>;
  hiddenTags: ReadonlySet<string>;
  matcher: Matcher;
}

const REGEXP_SPECIALS = /[.*+?^${}()|[\]\\]/g;

function escapeRegExp(text: string): string {
  return text.replace(REGEXP_SPECIALS, '\\$&');
}

/**
 * 创建匹配器。编译一次正则，test 与 segments 共用同一个实例（两者都先复位游标，
 * 不存在交错使用），避免逐条重建正则。
 */
export function createMatcher(query: string, useRegex: boolean): Matcher {
  if (!query) {
    return {
      active: false,
      invalid: false,
      test: () => true,
      segments: (text) => [{ text, hit: false }],
    };
  }

  let scanner: RegExp;
  let invalid = false;
  try {
    scanner = new RegExp(useRegex ? query : escapeRegExp(query), 'g');
  } catch {
    // 正则模式但表达式非法：与 UI 标红一致地降级为字面量匹配
    invalid = true;
    scanner = new RegExp(escapeRegExp(query), 'g');
  }

  return {
    active: true,
    invalid,
    test(text) {
      scanner.lastIndex = 0;
      return scanner.test(text);
    },
    segments(text) {
      const segments: HighlightSegment[] = [];
      let last = 0;
      let match: RegExpExecArray | null;
      scanner.lastIndex = 0;
      while ((match = scanner.exec(text)) !== null) {
        if (match[0] === '') {
          scanner.lastIndex++; // 空匹配不推进游标会死循环
          continue;
        }
        if (match.index > last) segments.push({ text: text.slice(last, match.index), hit: false });
        segments.push({ text: match[0], hit: true });
        last = match.index + match[0].length;
      }
      if (last < text.length) segments.push({ text: text.slice(last), hit: false });
      return segments;
    },
  };
}

export function accepts<E>(
  entry: E,
  rules: LogPanelRules<E>,
  hooks: LogPanelHooks<E> | undefined,
  state: FilterState,
): boolean {
  if (!state.levels.has(rules.level(entry))) return false;
  if (state.hiddenTags.has(rules.tag(entry))) return false;
  if (state.matcher.active && !state.matcher.test(entryText(entry, rules, hooks))) return false;
  return true;
}

/** 去重后的模块列表（排序保证筛选项顺序稳定） */
export function collectTags<E>(entries: readonly E[], rules: LogPanelRules<E>): string[] {
  const tags = new Set<string>();
  for (const entry of entries) tags.add(rules.tag(entry));
  return [...tags].sort();
}

/** 各等级条数，下标即等级（越界等级忽略） */
export function countLevels<E>(entries: readonly E[], rules: LogPanelRules<E>, size = 4): number[] {
  const counts = new Array<number>(size).fill(0);
  for (const entry of entries) {
    const level = rules.level(entry);
    if (level >= 0 && level < size) counts[level] += 1;
  }
  return counts;
}