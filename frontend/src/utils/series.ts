/**
 * 序列处理纯函数：降采样、速率差分、sparkline 坐标、统计。
 *
 * 队列页行内趋势图的数据管线：增量缓冲 → 降采样 → 坐标串，全部在此实现（可单测），
 * 组件只负责把坐标串画成 <polyline>。
 *
 * 关键约定：x 轴按**时间戳**映射（不是按数组下标等距铺开）——探测失败期间没有采样点
 * 或采到 null 时必须留出真实的时间空白，否则掉线会被视觉抹平。
 */
import type { SeriesPoint } from '@/types/api';

/** 渲染点数上限：320px 宽的图约 5px/点，再多看不出差别 */
export const MAX_SPARK_POINTS = 60;

/** 点序列的数值（null 表示该点无有效值，渲染时断开） */
export type NumericPoint = [number, number | null];

/**
 * 等距降采样（桶内取最后一点）。
 *
 * 取"最后一点"而非均值：均值会把失败点（null）洗掉、也会削平尖峰；取桶内最后一点
 * 保留该桶结束时段的真实状态（含 null），失败期因此仍表现为断线。
 */
export function downsample(points: NumericPoint[], max = MAX_SPARK_POINTS): NumericPoint[] {
  if (points.length <= max) return points;
  const out: NumericPoint[] = [];
  const step = points.length / max;
  for (let i = 0; i < max; i++) {
    const idx = Math.min(points.length - 1, Math.floor((i + 1) * step) - 1);
    out.push(points[Math.max(0, idx)]);
  }
  return out;
}

/** 累计值序列 → 速率序列（字节/秒）：相邻点差分除以时间间隔；时间不前进的点丢弃 */
export function toRates(samples: [number, number][]): [number, number][] {
  const out: [number, number][] = [];
  for (let i = 1; i < samples.length; i++) {
    const dt = samples[i][0] - samples[i - 1][0];
    if (dt <= 0) continue;
    out.push([samples[i][0], (samples[i][1] - samples[i - 1][1]) / dt]);
  }
  return out;
}

export interface SparkStats {
  total: number;      // 窗口内探测次数
  ok: number;         // 成功次数
  rate: number | null; // 成功率 0-100（无探测为 null）
}

/** 延时序列统计：成功率 = 非 null 点占比 */
export function sparkStats(points: NumericPoint[]): SparkStats {
  const total = points.length;
  const ok = points.reduce((n, p) => n + (p[1] !== null ? 1 : 0), 0);
  return { total, ok, rate: total ? Math.round((ok / total) * 100) : null };
}

export interface SparkOptions {
  width: number;
  height: number;
  /** 对数 y 轴（延迟跨数量级时用，与仪表盘延迟趋势图一致） */
  log?: boolean;
  /** 上下留白（px），避免峰值贴边 */
  pad?: number;
}

/**
 * 坐标 → SVG polyline points 串（断点切段），多序列共用一套坐标映射。
 *
 * 返回与入参一一对应的二维数组：外层是序列，内层是"连续段"——null 点把序列切成多段，
 * 组件把每段画成一条 polyline，失败期即呈现为断开的折线。空段不返回。
 *
 * 多序列共用一个 y 值域与一条时间轴：上下行速率之类需要相互比较的曲线不能各自缩放。
 */
export function sparkSegments(series: NumericPoint[][], opts: SparkOptions): string[][] {
  const { width, height, log = false, pad = 2 } = opts;
  const all = series.flat().filter((p): p is [number, number] => p[1] !== null);
  if (all.length === 0) return series.map(() => []);

  const t0 = Math.min(...all.map((p) => p[0]));
  const t1 = Math.max(...all.map((p) => p[0]));
  const span = t1 - t0;
  const values = all.map((p) => p[1]);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const innerH = Math.max(1, height - pad * 2);
  const la = Math.log10(Math.max(lo, 1e-4));
  const lb = Math.log10(Math.max(hi, 1e-4));

  const xOf = (ts: number) => (span > 0 ? ((ts - t0) / span) * width : width);
  const yOf = (v: number) => {
    // 对数轴：延迟恒 > 0，下界兜到 1e-4 避免 log(0)；同值序列退化为中线
    const k = log
      ? lb > la ? (Math.log10(Math.max(v, 1e-4)) - la) / (lb - la) : 0.5
      : hi > lo ? (v - lo) / (hi - lo) : 0.5;
    return height - pad - k * innerH;
  };

  return series.map((points) => {
    const segments: string[] = [];
    let current: string[] = [];
    for (const [ts, v] of points) {
      if (v === null) {
        if (current.length) segments.push(current.join(' '));
        current = [];
        continue;
      }
      current.push(`${xOf(ts).toFixed(1)},${yOf(v).toFixed(1)}`);
    }
    if (current.length) segments.push(current.join(' '));
    return segments;
  });
}

/** 去掉重复时间戳的点（增量拼接时同一 ts 可能被下发两次），保持时间升序 */
export function dedupeByTs(points: SeriesPoint[]): SeriesPoint[] {
  const out: SeriesPoint[] = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (last && p[0] <= last[0]) continue;
    out.push(p);
  }
  return out;
}

/** 按时间窗口裁剪（早于 cutoff 的点丢弃） */
export function trimBefore<T extends [number, unknown]>(points: T[], cutoff: number): T[] {
  let i = 0;
  while (i < points.length && points[i][0] < cutoff) i++;
  return i === 0 ? points : points.slice(i);
}
