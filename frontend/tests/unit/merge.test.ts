/** 历史合并/游标与队列顺序冻结合并：与旧前端语义一致 */
import { describe, expect, it } from 'vitest';
import { historyCursor, mergePoints } from '@/stores/status';
import { mergeKeepOrder } from '@/utils/merge';

describe('mergePoints', () => {
  it('空增量原样返回', () => {
    const a = [{ ts: 1 }];
    expect(mergePoints(a, [])).toBe(a);
  });
  it('按时间序追加', () => {
    expect(mergePoints([{ ts: 1 }, { ts: 2 }], [{ ts: 3 }])).toEqual([{ ts: 1 }, { ts: 2 }, { ts: 3 }]);
  });
  it('超限裁剪保留最新（保留尾部 max 个）', () => {
    const existing = Array.from({ length: 5 }, (_, i) => ({ ts: i }));
    const merged = mergePoints(existing, [{ ts: 100 }, { ts: 101 }], 5);
    expect(merged.map((p) => p.ts)).toEqual([2, 3, 4, 100, 101]);
  });
});

describe('historyCursor', () => {
  it('取两序列最后点最大 ts，空为 0', () => {
    expect(historyCursor([], [])).toBe(0);
    expect(historyCursor([{ ts: 5 }], [{ ts: 9 }, { ts: 7 }])).toBe(7);
    expect(historyCursor([{ ts: 12 }], [{ ts: 7 }])).toBe(12);
  });
});

describe('mergeKeepOrder', () => {
  const key = (p: { proxy: string }) => p.proxy;
  it('旧顺序保留、踢除消失项、新项追加末尾', () => {
    const oldList = [{ proxy: 'a' }, { proxy: 'b' }, { proxy: 'c' }];
    const newList = [{ proxy: 'c' }, { proxy: 'a' }, { proxy: 'd' }];
    expect(mergeKeepOrder(oldList, newList, key).map((p) => p.proxy)).toEqual(['a', 'c', 'd']);
  });
  it('数据更新以新对象替换（旧对象丢弃）', () => {
    const oldList = [{ proxy: 'a', v: 1 }];
    const newList = [{ proxy: 'a', v: 2 }];
    expect(mergeKeepOrder(oldList, newList, key)[0].v).toBe(2);
  });
});
