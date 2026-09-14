/** 序列纯函数：降采样、速率差分、sparkline 坐标（时间戳映射/断线/多序列共享值域）、统计 */
import { describe, expect, it } from 'vitest';
import {
  MAX_SPARK_POINTS,
  dedupeByTs,
  downsample,
  sparkSegments,
  sparkStats,
  toRates,
  trimBefore,
  type NumericPoint,
} from '@/utils/series';

describe('downsample', () => {
  it('不超过上限时原样返回', () => {
    const pts: NumericPoint[] = [[1, 0.1], [2, 0.2]];
    expect(downsample(pts, 5)).toBe(pts);
  });

  it('超上限时按桶取最后一点（保留 null，失败期不被洗掉）', () => {
    const pts: NumericPoint[] = Array.from({ length: 10 }, (_, i) => [i, i === 9 ? null : i / 10]);
    const out = downsample(pts, 5);
    expect(out).toHaveLength(5);
    expect(out[out.length - 1]).toEqual([9, null]);
  });

  it('默认上限为 MAX_SPARK_POINTS', () => {
    const pts: NumericPoint[] = Array.from({ length: 500 }, (_, i) => [i, i]);
    expect(downsample(pts)).toHaveLength(MAX_SPARK_POINTS);
  });
});

describe('toRates', () => {
  it('累计值差分除以时间间隔', () => {
    expect(toRates([[0, 100], [2, 300], [4, 400]])).toEqual([[2, 100], [4, 50]]);
  });

  it('时间不前进的相邻点跳过（差分基准是紧邻前一点）', () => {
    // [0,10] 与 [0,0] 时间相同 → 该间隔跳过；[2,20] 与前一点 [0,10] 差分 = (20-10)/2
    expect(toRates([[0, 0], [0, 10], [2, 20]])).toEqual([[2, 5]]);
  });

  it('单点/空序列无速率（首点没有参照）', () => {
    expect(toRates([[0, 5]])).toEqual([]);
    expect(toRates([])).toEqual([]);
  });
});

describe('sparkStats', () => {
  it('成功率 = 非 null 点占比，含失败点', () => {
    const stats = sparkStats([[1, 0.2], [2, null], [3, 0.3], [4, null]]);
    expect(stats).toEqual({ total: 4, ok: 2, rate: 50 });
  });

  it('窗口内无探测时成功率为 null（不显示 0%）', () => {
    expect(sparkStats([])).toEqual({ total: 0, ok: 0, rate: null });
  });
});

describe('sparkSegments', () => {
  const opts = { width: 100, height: 20 };

  it('按时间戳映射 x 轴（时间跨度均匀则首尾贴边）', () => {
    const [seg] = sparkSegments([[[0, 1], [10, 2]]], opts);
    expect(seg).toHaveLength(1);
    const xs = seg[0].split(' ').map((p) => Number(p.split(',')[0]));
    expect(xs).toEqual([0, 100]);
  });

  it('时间戳不均匀时按真实时间留白（不是按数组下标等距）', () => {
    // 第 2、3 点间隔 90s，第 1、2 点间隔 10s：中间点应靠近左侧
    const [seg] = sparkSegments([[[0, 1], [10, 1], [100, 1]]], opts);
    const xs = seg[0].split(' ').map((p) => Number(p.split(',')[0]));
    expect(xs[1]).toBeCloseTo(10);
    expect(xs[2]).toBeCloseTo(100);
  });

  it('null 点切断为多段（失败期呈现断线）', () => {
    const [segs] = sparkSegments([[[0, 1], [1, 2], [2, null], [3, 3], [4, 4]]], opts);
    expect(segs).toHaveLength(2);
    expect(segs[0].split(' ')).toHaveLength(2);
    expect(segs[1].split(' ')).toHaveLength(2);
  });

  it('全 null / 空序列返回空段', () => {
    expect(sparkSegments([[[0, null], [1, null]]], opts)).toEqual([[]]);
    expect(sparkSegments([[]], opts)).toEqual([[]]);
  });

  it('多序列共用值域与时间轴（上下行速率可相互比较）', () => {
    const segs = sparkSegments([[[0, 10], [10, 10]], [[0, 0], [10, 20]]], opts);
    expect(segs).toHaveLength(2);
    // 上界 20 → 值 10 落在纵向中部；两序列的第一个点时间相同 → 同一条 x
    const yTop = Number(segs[0][0].split(' ')[0].split(',')[1]);
    const yBottom = Number(segs[1][0].split(' ')[0].split(',')[1]);
    expect(yTop).toBeLessThan(yBottom);
  });

  it('对数轴把跨数量级的延迟摊开', () => {
    const [seg] = sparkSegments([[[0, 0.1], [1, 1], [2, 10]]], { ...opts, log: true });
    const ys = seg[0].split(' ').map((p) => Number(p.split(',')[1]));
    // 三个点在 log 轴上是等距的
    expect(ys[1] - ys[2]).toBeCloseTo(ys[0] - ys[1], 1);
  });

  it('同值序列退化为中线（不除零）', () => {
    const [seg] = sparkSegments([[[0, 5], [1, 5]]], opts);
    const ys = seg[0].split(' ').map((p) => Number(p.split(',')[1]));
    expect(ys[0]).toBeCloseTo(10);
    expect(ys[1]).toBeCloseTo(10);
  });
});

describe('dedupeByTs', () => {
  it('丢弃非递增时间戳的重复点（增量拼接边界）', () => {
    expect(dedupeByTs([[1, 0.1], [2, 0.2], [2, 0.2], [3, null]])).toEqual([[1, 0.1], [2, 0.2], [3, null]]);
  });

  it('乱序回退的点按"时间不前进"丢弃，保持缓冲单调', () => {
    expect(dedupeByTs([[2, 1], [1, 2], [3, 3]])).toEqual([[2, 1], [3, 3]]);
  });
});

describe('trimBefore', () => {
  it('丢弃早于 cutoff 的点，全部过期时返回空', () => {
    expect(trimBefore([[1, 0], [5, 0], [9, 0]], 5)).toEqual([[5, 0], [9, 0]]);
    expect(trimBefore([[1, 0], [2, 0]], 5)).toEqual([]);
  });

  it('无需裁剪时返回原数组引用（避免无谓重建）', () => {
    const pts: [number, number][] = [[10, 0]];
    expect(trimBefore(pts, 5)).toBe(pts);
  });
});
