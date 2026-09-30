/** 序列纯函数：降采样、速率差分、sparkline 坐标（共享时间窗口/断线/多序列共享值域）、统计 */
import { describe, expect, it } from 'vitest';
import {
  MAX_SPARK_POINTS,
  dedupeByTs,
  downsample,
  matchPrevAvg,
  seriesTimeWindow,
  sparkDomain,
  sparkSegments,
  sparkStats,
  toRates,
  trimBefore,
  trimWith,
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

  it('零速率置 null（不绘制、不参与 y 值域；负值同样丢弃）', () => {
    expect(toRates([[0, 10], [2, 10], [4, 25], [6, 25], [8, 5]])).toEqual([
      [2, null], [4, 7.5], [6, null], [8, null],
    ]);
  });
});

describe('sparkDomain', () => {
  it('多序列共用值域，只统计非 null 点', () => {
    expect(sparkDomain([[[0, 3], [1, null]], [[0, 10], [1, 1]]])).toEqual({ lo: 1, hi: 10 });
  });

  it('无任何可绘制点时返回 null（调用方显示占位而不是画空线）', () => {
    expect(sparkDomain([])).toBeNull();
    expect(sparkDomain([[[0, null]]])).toBeNull();
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

describe('seriesTimeWindow', () => {
  it('起 = 最早采样点，止 = 现在', () => {
    const bufs = [{ lat: [[100, 1], [104, 2]] as NumericPoint[], up: [[102, 1]] as NumericPoint[], down: [] }];
    expect(seriesTimeWindow(bufs, 110)).toEqual([100, 110]);
  });

  it('止与最末采样取大（容忍服务端时钟略快于浏览器）', () => {
    const bufs = [{ lat: [[100, 1], [104, 2]] as NumericPoint[], up: [], down: [] }];
    expect(seriesTimeWindow(bufs, 101)).toEqual([100, 104]);
  });

  it('跨行取全局最早点（行与行共用同一原点）', () => {
    const bufs = [
      { lat: [[100, 1]] as NumericPoint[], up: [], down: [] },
      { lat: [[90, 1], [95, 1]] as NumericPoint[], up: [], down: [] },
    ];
    expect(seriesTimeWindow(bufs, 120)).toEqual([90, 120]);
  });

  it('无任何采样时退化为 [now, now]（组件显示占位而非画线）', () => {
    expect(seriesTimeWindow([], 50)).toEqual([50, 50]);
    expect(seriesTimeWindow([{ lat: [], up: [], down: [] }], 50)).toEqual([50, 50]);
  });
});

describe('sparkSegments', () => {
  const opts = (window: [number, number]) => ({ width: 100, height: 20, window });
  const xs = (seg: string) => seg.split(' ').map((p) => Number(p.split(',')[0]));
  const ys = (seg: string) => seg.split(' ').map((p) => Number(p.split(',')[1]));

  it('按时间戳映射 x 轴（点落在窗口两端则首尾贴边）', () => {
    const [seg] = sparkSegments([[[0, 1], [10, 2]]], opts([0, 10]));
    expect(seg).toHaveLength(1);
    expect(xs(seg[0])).toEqual([0, 100]);
  });

  it('窗口比数据跨度大时留白（刚入队/采样已停止的行不再被拉满整宽）', () => {
    const [seg] = sparkSegments([[[0, 1], [10, 2]]], opts([0, 40]));
    expect(xs(seg[0])).toEqual([0, 25]);
  });

  it('多序列共用窗口（同一时刻落在同一条 x 上，可相互比较）', () => {
    const segs = sparkSegments([[[0, 1], [10, 1]], [[5, 1], [10, 1]]], opts([0, 10]));
    expect(xs(segs[0][0])).toEqual([0, 100]);
    expect(xs(segs[1][0])[0]).toBe(50);
    expect(xs(segs[1][0])[1]).toBe(100);
  });

  it('时间戳不均匀时按真实时间留白（不是按数组下标等距）', () => {
    // 第 2、3 点间隔 90s，第 1、2 点间隔 10s：中间点应靠近左侧
    const [seg] = sparkSegments([[[0, 1], [10, 1], [100, 1]]], opts([0, 100]));
    expect(xs(seg[0])[1]).toBeCloseTo(10);
    expect(xs(seg[0])[2]).toBeCloseTo(100);
  });

  it('null 点切断为多段（失败期呈现断线）', () => {
    const [segs] = sparkSegments([[[0, 1], [1, 2], [2, null], [3, 3], [4, 4]]], opts([0, 4]));
    expect(segs).toHaveLength(2);
    expect(segs[0].split(' ')).toHaveLength(2);
    expect(segs[1].split(' ')).toHaveLength(2);
  });

  it('孤立单点补重复坐标（单点 polyline 浏览器不绘制，零长度段才出圆点）', () => {
    const [segs] = sparkSegments([[[0, 1], [1, null], [2, 2], [3, null]]], opts([0, 3]));
    expect(segs).toHaveLength(2);
    for (const seg of segs) {
      const [a, b] = seg.split(' ');
      expect(seg.split(' ')).toHaveLength(2);
      expect(a).toBe(b); // 同一坐标两遍：零长度子路径的圆头端帽才渲染出那个点
    }
    expect(segs[0]).not.toBe(segs[1]);
  });

  it('全 null / 空序列返回空段', () => {
    expect(sparkSegments([[[0, null], [1, null]]], opts([0, 1]))).toEqual([[]]);
    expect(sparkSegments([[]], opts([0, 1]))).toEqual([[]]);
  });

  it('多序列共用值域（上下行速率可相互比较）', () => {
    const segs = sparkSegments([[[0, 10], [10, 10]], [[0, 0], [10, 20]]], opts([0, 10]));
    expect(segs).toHaveLength(2);
    // 上界 20 → 值 10 落在纵向中部；两序列的第一个点时间相同 → 同一条 x
    expect(ys(segs[0][0])[0]).toBeLessThan(ys(segs[1][0])[0]);
  });

  it('对数轴把跨数量级的延迟摊开', () => {
    const [seg] = sparkSegments([[[0, 0.1], [1, 1], [2, 10]]], { ...opts([0, 2]), log: true });
    const y = ys(seg[0]);
    // 三个点在 log 轴上是等距的
    expect(y[1] - y[2]).toBeCloseTo(y[0] - y[1], 1);
  });

  it('同值序列退化为中线（不除零）', () => {
    const [seg] = sparkSegments([[[0, 5], [1, 5]]], opts([0, 1]));
    expect(ys(seg[0])).toEqual([10, 10]);
  });

  it('窗口退化（起止相同）不产生 NaN 坐标', () => {
    const [seg] = sparkSegments([[[7, 1], [7, 2]]], opts([7, 7]));
    expect(seg[0]).not.toContain('NaN');
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

describe('trimWith', () => {
  it('按 key 取时间戳裁剪（对象元素：历史合并复用）', () => {
    const pts = [{ ts: 1 }, { ts: 5 }, { ts: 9 }];
    expect(trimWith(pts, 5, (p) => p.ts)).toEqual([{ ts: 5 }, { ts: 9 }]);
    expect(trimWith(pts, 100, (p) => p.ts)).toEqual([]);
  });

  it('无需裁剪时返回原数组引用（与 trimBefore 同一语义）', () => {
    const pts = [{ ts: 10 }];
    expect(trimWith(pts, 5, (p) => p.ts)).toBe(pts);
  });
});

describe('matchPrevAvg', () => {
  const refs = [
    { ts: 100, avg: 0.2 },
    { ts: 102, avg: 0.3 },
    { ts: 104, avg: 0.4 },
  ];

  it('为每个时间点匹配最近的前一个参考点', () => {
    expect(matchPrevAvg(refs, [101, 103, 105])).toEqual([0.2, 0.3, 0.4]);
  });

  it('参考点时间戳相同/略早时命中（同轮采集 ts 不精确相等）', () => {
    expect(matchPrevAvg(refs, [102.5, 103.9])).toEqual([0.3, 0.3]);
  });

  it('参考点之前无点、或早于一个采样周期以上 → null（不绘制）', () => {
    expect(matchPrevAvg(refs, [99, 100, 104 + 16])).toEqual([null, 0.2, null]);
  });

  it('参考序列为空时全 null', () => {
    expect(matchPrevAvg([], [1, 2])).toEqual([null, null]);
  });

  it('容差随参考序列采样周期自适应：锁定态 30s 间隔不漏配', () => {
    // 锁定单一上游时心跳放宽到 30s，参考点间隔 30s；流量仍每 2s 采一个点。
    // 容差若写死小常数（曾为 15），每个 30s 区间只有前一半能匹配上，延迟线断成虚线。
    const locked = [
      { ts: 0, avg: 0.3 },
      { ts: 30, avg: 0.4 },
      { ts: 60, avg: 0.5 },
    ];
    const trafficTs: number[] = [];
    for (let t = 2; t <= 60; t += 2) trafficTs.push(t);
    const out = matchPrevAvg(locked, trafficTs);
    expect(out.filter((v) => v !== null)).toHaveLength(trafficTs.length);
    expect(out[out.length - 1]).toBe(0.5);
  });

  it('容差只覆盖一个采样周期：参考序列停更后不把陈旧值当最新值（尾部为 null）', () => {
    const locked = [
      { ts: 0, avg: 0.3 },
      { ts: 30, avg: 0.4 },
    ];
    // 参考点间隔 30s（周期=30）：31s 距末点 1s 仍算最新值；
    // 61s 距末点已 31s > 一个周期 → 不再匹配
    expect(matchPrevAvg(locked, [20, 29, 31, 61])).toEqual([0.3, 0.3, 0.4, null]);
  });

  it('参考点少于 2 个时推断不出周期 → 只认时间戳完全重合的点（不硬凑趋势）', () => {
    // 周期为 0：非重合点一律不匹配。实际 ts 为浮点秒，几乎不可能重合，
    // 故只有一个采样点时图上不会画出延迟线，等第二个点到达才开始成线。
    expect(matchPrevAvg([{ ts: 100, avg: 0.2 }], [99, 100, 102])).toEqual([null, 0.2, null]);
  });

  it('参考值本身为 null 时该点不绘制', () => {
    const withNull = [
      { ts: 100, avg: null },
      { ts: 102, avg: 0.3 },
    ];
    expect(matchPrevAvg(withNull, [100, 101, 102])).toEqual([null, null, 0.3]);
  });
});
