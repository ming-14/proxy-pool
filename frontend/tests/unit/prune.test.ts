import { describe, expect, it } from 'vitest';
import { pruneMap } from '@/utils/prune';

describe('pruneMap', () => {
  it('丢弃不在保留集中的 key', () => {
    const m = new Map([
      ['a', 1],
      ['b', 2],
      ['c', 3],
    ]);
    pruneMap(m, ['a', 'c']);
    expect([...m.keys()]).toEqual(['a', 'c']);
  });

  it('保留集为空即清空', () => {
    const m = new Map([['a', 1]]);
    pruneMap(m, []);
    expect(m.size).toBe(0);
  });

  it('保留集含不存在的 key 不影响其余条目', () => {
    const m = new Map([['a', 1]]);
    pruneMap(m, ['a', 'ghost']);
    expect([...m.keys()]).toEqual(['a']);
  });
});
