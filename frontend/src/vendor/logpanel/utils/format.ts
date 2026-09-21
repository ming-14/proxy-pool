/**
 * 展示文本格式化：时间、纯文本行、导出文件名，以及 hooks.formatText 的统一入口。
 */
import type { LogPanelHooks, LogPanelRules } from '../types';

/** 毫秒时间戳 → HH:MM:SS.mmm（与后端控制台日志时间格式一致） */
export function formatTime(ts: number): string {
  const date = new Date(ts);
  const pad = (value: number) => String(value).padStart(2, '0');
  const ms = String(date.getMilliseconds()).padStart(3, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${ms}`;
}

/** 条目展示文本：搜索、高亮、复制、导出共用，保证四处看到的是同一串 */
export function entryText<E>(entry: E, rules: LogPanelRules<E>, hooks?: LogPanelHooks<E>): string {
  const text = rules.text(entry);
  if (!hooks?.formatText) return text;
  try {
    return hooks.formatText(text, entry);
  } catch (error) {
    console.error('[logpanel] formatText 钩子抛出异常，回退原始文本', error);
    return text;
  }
}

/** 复制/导出的纯文本行 */
export function formatEntryPlain<E>(entry: E, rules: LogPanelRules<E>, hooks?: LogPanelHooks<E>): string {
  return `${rules.tsStr(entry)} [${rules.levelName(entry)}] [${rules.tag(entry)}] ${entryText(entry, rules, hooks)}`;
}

/** 导出文件名：logs-YYYYMMDD-HHMMSS.txt */
export function exportFileName(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return (
    `logs-${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}.txt`
  );
}