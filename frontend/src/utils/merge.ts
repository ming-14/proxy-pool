/**
 * 队列顺序冻结合并：选中/拖拽/悬浮期间保持旧顺序（数据更新但行不跑位），
 * 被踢出的移除，新入队的追加末尾。纯函数，可单测。
 */
export function mergeKeepOrder<T extends Record<string, unknown>>(
  oldList: T[],
  newList: T[],
  key: (item: T) => string,
): T[] {
  const newMap = new Map(newList.map((p) => [key(p), p]));
  const result: T[] = [];
  const seen = new Set<string>();
  for (const p of oldList) {
    const fresh = newMap.get(key(p));
    if (fresh) {
      result.push(fresh);
      seen.add(key(p));
    }
  }
  for (const p of newList) {
    if (!seen.has(key(p))) result.push(p);
  }
  return result;
}
