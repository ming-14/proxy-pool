/**
 * Map 淘汰：丢弃 key 不在 keep 中的条目。纯函数，可单测。
 *
 * keep 为空即清空（`refreshStandbySeries` 依赖此语义：队列清空 → 缓冲全清）。
 * 把空集当"数据未就绪、不动缓存"的调用方须自行先判空（见 refreshStandbyGeo）。
 */
export function pruneMap<K, V>(map: Map<K, V>, keep: Iterable<K>): void {
  const keepSet = new Set(keep);
  for (const key of [...map.keys()]) {
    if (!keepSet.has(key)) map.delete(key);
  }
}
