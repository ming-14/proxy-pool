/**
 * 序列处理纯函数：降采样、速率差分、sparkline 坐标、统计。
 *
 * 队列页行内趋势图的数据管线：增量缓冲 → 降采样 → 坐标串，全部在此实现（可单测），
 * 组件只负责把坐标串画成 <polyline>。
 *
 * 关键约定：x 轴按**时间戳**映射（不是按数组下标等距铺开）——探测失败期间没有采样点
 * 或采到 null 时必须留出真实的时间空白，否则掉线会被视觉抹平；时间窗口由调用方
 * 统一给定（`seriesTimeWindow`）而不是按各自点集归一化，否则新入队/已陈旧的行分不出来。
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

/**
 * 累计值序列 → 速率序列（字节/秒）：相邻点差分除以时间间隔；时间不前进的点丢弃。
 *
 * 非正速率（该间隔没有流量）一律置 null：**不绘制、也不参与 y 值域**。
 * 不这样做时，"零流量"会画成一条平线（全零时值域退化、线落在中间），看着像有数据，
 * 还会把真正有流量的尖峰压平；与仪表盘流量图的规则保持一致。
 */
export function toRates(samples: [number, number][]): NumericPoint[] {
  const out: NumericPoint[] = [];
  for (let i = 1; i < samples.length; i++) {
    const dt = samples[i][0] - samples[i - 1][0];
    if (dt <= 0) continue;
    const rate = (samples[i][1] - samples[i - 1][1]) / dt;
    out.push([samples[i][0], rate > 0 ? rate : null]);
  }
  return out;
}

export interface SparkDomain {
  lo: number;
  hi: number;
}

/**
 * y 值域（多序列共用）：只统计非 null 点；无任何可绘制点时返回 null。
 * 值域是「坐标映射」与「左侧标出的数据值」的同一来源，避免两边各算一套而对不上。
 */
export function sparkDomain(series: NumericPoint[][]): SparkDomain | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const pts of series) {
    for (const [, v] of pts) {
      if (v === null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  return Number.isFinite(lo) ? { lo, hi } : null;
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
  /**
   * 时间窗口 [起, 止]（秒）：由调用方统一提供，全部行（及同一行的多条曲线）共用。
   * 不能按各自点集归一化——那会让"刚入队只有两点"和"采样已停止"的行都铺满整宽，
   * 新老行看着一样、陈旧数据也贴着右边缘，横向比较随之失效。
   */
  window: [number, number];
  /** 对数 y 轴（延迟跨数量级时用，与仪表盘延迟趋势图一致） */
  log?: boolean;
  /** 上下留白（px），避免峰值贴边 */
  pad?: number;
  /**
   * 预先算好的 y 值域（可选）。传入则不再自行遍历求值域——调用方若还要用同一值域
   * 标注数据值区间，就可只算一次；不传时内部按 sparkDomain 计算（行为不变）。
   */
  domain?: SparkDomain | null;
}

/** 行内趋势图的数据缓冲视图（时间段）——只读首末两点，故只要求时间序 */
export interface TimeBuffers {
  lat: SeriesPoint[];
  up: SeriesPoint[];
  down: SeriesPoint[];
}

/**
 * 共享时间窗口 [起, 止]（秒）：起 = 所有缓冲最早的采样点，止 = 现在。
 * 止取「现在」而非最末采样：采样已停止的行因此停在右边缘左侧（陈旧可见），
 * 而不是被拉伸到右边缘装成最新；与最末采样取大是为了容忍服务端时钟略快。
 */
export function seriesTimeWindow(buffers: Iterable<TimeBuffers>, now: number): [number, number] {
  let start = Infinity;
  let end = now;
  for (const buf of buffers) {
    for (const pts of [buf.lat, buf.up, buf.down]) {
      if (!pts.length) continue;
      if (pts[0][0] < start) start = pts[0][0];
      if (pts[pts.length - 1][0] > end) end = pts[pts.length - 1][0];
    }
  }
  return [Number.isFinite(start) ? start : now, end];
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
  const { width, height, window: [t0, t1], log = false, pad = 2 } = opts;
  // 调用方可传预算值域（同一值域还要用于标注数据值，避免重复遍历）；缺省自行计算
  const domain = opts.domain !== undefined ? opts.domain : sparkDomain(series);
  if (!domain) return series.map(() => []);

  const span = t1 - t0;
  const { lo, hi } = domain;
  const innerH = Math.max(1, height - pad * 2);
  const la = Math.log10(Math.max(lo, 1e-4));
  const lb = Math.log10(Math.max(hi, 1e-4));

  const xOf = (ts: number) => (span > 0 ? ((ts - t0) / span) * width : 0);
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
    const flush = () => {
      // 单点段要写两遍同一坐标：只有一个点的 <polyline> 不产生可描边子路径，
      // 浏览器什么都不画（实测），零长度段的圆头端帽才会显出那个孤立的成功点。
      if (current.length === 1) current.push(current[0]);
      if (current.length) segments.push(current.join(' '));
      current = [];
    };
    for (const [ts, v] of points) {
      if (v === null) {
        flush();
        continue;
      }
      current.push(`${xOf(ts).toFixed(1)},${yOf(v).toFixed(1)}`);
    }
    flush();
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

/**
 * 参考序列的采样周期（秒）= 相邻采样点间隔的最大值。
 *
 * 作匹配容差的依据：一个延迟读数在下一次采样到来前都是"最新值"，故容差取该序列
 * 自身的一个采样周期。不能写死常数——采样节奏随运行状态变（心跳 2s；锁定单一
 * 上游时放宽到 30s），写死小常数会在锁定态漏配（30s 间隔只覆盖前一半流量点，
 * 延迟线画成断续虚线），写死大常数又会在空闲时把陈旧值当最新值画出去。
 * 少于 2 个参考点时无法推断周期，返回 0（不匹配，不硬凑趋势）。
 */
function refPeriod(refs: { ts: number }[]): number {
  let period = 0;
  for (let i = 1; i < refs.length; i++) {
    const gap = refs[i].ts - refs[i - 1].ts;
    if (gap > period) period = gap;
  }
  return period;
}

/**
 * 为每个时间点匹配"最近的前一个"参考点（两序列均需时间升序，游标单次扫描）。
 * 用于把流量差分点对齐到队列采样点：两序列采样节奏不同（流量固定 2s 独立采样，
 * 队列指标挂在 heartbeat 轮次上、锁定单一上游时放宽到 30s），不能按索引对齐
 * ——下标会错位，且同轮采集的 ts 也不精确相等（standby 在 heartbeat 内采样、
 * traffic 在其后）。参考点早于该点超过一个采样周期（`refPeriod`）、或参考值
 * 本身为 null，都返回 null（不绘制）。
 */
export function matchPrevAvg(
  refs: { ts: number; avg: number | null }[],
  ts: number[],
): (number | null)[] {
  const period = refPeriod(refs);
  let i = 0;
  return ts.map((t) => {
    while (i < refs.length && refs[i].ts <= t) i++;
    const p = i > 0 ? refs[i - 1] : null;
    return p && t - p.ts <= period ? p.avg : null;
  });
}

/** 按时间窗口裁剪：早于 cutoff 的点丢弃（key 取该点时间戳；无需裁剪返回原引用） */
export function trimWith<T>(points: T[], cutoff: number, key: (p: T) => number): T[] {
  let i = 0;
  while (i < points.length && key(points[i]) < cutoff) i++;
  return i === 0 ? points : points.slice(i);
}

/** 按时间窗口裁剪（点形如 [ts, value]） */
export function trimBefore<T extends [number, unknown]>(points: T[], cutoff: number): T[] {
  return trimWith(points, cutoff, (p) => p[0]);
}
