/**
 * tag 配色：先查宿主/内置表，未命中则按字符串哈希出稳定 HSL 色。
 *
 * 哈希保证同一 tag 每次得到同一颜色、不同 tag 尽量分散，宿主无需为每个模块登记颜色。
 */

/** 字符串 → 稳定 HSL 颜色 */
function hashColor(text: string): string {
  let hash = 0;
  for (let i = 0; i < text.length; i++) {
    hash = (hash * 31 + text.charCodeAt(i)) | 0;
  }
  return `hsl(${Math.abs(hash) % 360}, 65%, 55%)`;
}

export function resolveTagColor(tag: string, table: Record<string, string>): string {
  return table[tag] ?? hashColor(tag);
}