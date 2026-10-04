/**
 * 3D 地球数据构建（纯函数，Vitest 直测）：把 /api/globe 快照映射为 globe.gl 的点位与弧。
 *
 * 视觉口径（LLD §4.3）：线宽 ∝ 聚合 EMA 速率、光点流速 ∝ 瞬时速率；两段弧——
 * 本机 → 代理（节点聚合速率）与 代理 → 目标城市（flows 分组，起点取节点坐标，
 * bind 不在当前节点集或无坐标不产弧）。速率映射取对数归一：单调、上下界固定；
 * 空数据/缺坐标安全返回，不产 NaN。
 */
import type { GlobeData, GlobeFlow, GlobeNode } from '@/types/api';
import { escapeHtml, fmtBytesSpaced, fmtLatency } from '@/utils/format';

/** 速率 → 弧线宽区间（globe.gl 弧宽单位；对数归一后线性映射） */
export const MIN_STROKE = 0.15;
export const MAX_STROKE = 0.9;
/** 速率 → dash 动画时长区间（ms）：速率越大动画越快（光点流动越快） */
export const DASH_TIME_SLOW_MS = 12_000;
export const DASH_TIME_FAST_MS = 800;
/** 流速映射的参考速率（B/s）：1MB/s 即达到最快（该映射不随画面最大值漂移） */
export const DASH_RATE_REF = 1_000_000;
/** 短 dash + 长 gap = 弧上单颗流动光点（globe.gl 的 dash 长度/间隔为弧长比例） */
export const ARC_DOT_STYLE = { dashLength: 0.02, dashGap: 0.96 } as const;

/** 数据层配色（由组件读 CSS 变量构建；弧色在此基础上叠透明度） */
export interface GlobePalette {
  self: string;      // 本机节点/第一段弧
  proxy: string;     // 正常代理节点
  locked: string;    // 锁定为唯一上游的代理
  failed: string;    // 有连续失败计数的代理
  target: string;    // 目标城市（第二段弧）
}

/** globe.gl 点位（accessor 按属性名直取） */
export interface GlobePoint {
  lat: number;
  lng: number;
  color: string;
  radius: number;
  altitude: number;
  label: string;   // HTML tooltip（文本已转义）
}

/** globe.gl 弧（两段弧共用同一形状；stroke 取聚合 EMA、dashTime 取瞬时速率） */
export interface GlobeArc {
  startLat: number;
  startLng: number;
  endLat: number;
  endLng: number;
  color: string;
  stroke: number;
  dashTime: number;
  label: string;
}

export interface GlobePointData {
  lat: number;
  lng: number;
}

/** 对数归一：0 → 0，≥ maxRate → 1；非法/负值/参考值无效时回 0（宁可最小，不给 NaN） */
function normalizeRate(rate: number, maxRate: number): number {
  if (!Number.isFinite(rate) || rate <= 0) return 0;
  if (!Number.isFinite(maxRate) || maxRate <= 0) return 0;
  if (rate >= maxRate) return 1;
  return Math.log1p(rate) / Math.log1p(maxRate);
}

/** 速率 → 弧线宽：0.15~0.9 单调递增；maxRate 为画面内最大速率（对数归一） */
export function rateToStroke(rate: number, maxRate: number): number {
  return MIN_STROKE + (MAX_STROKE - MIN_STROKE) * normalizeRate(rate, maxRate);
}

/** 速率 → dash 动画时长：800~12000ms 单调递减（速率越大越快） */
export function rateToDashTime(rate: number): number {
  return DASH_TIME_SLOW_MS
    - (DASH_TIME_SLOW_MS - DASH_TIME_FAST_MS) * normalizeRate(rate, DASH_RATE_REF);
}

/** 给 #rgb/#rrggbb 颜色叠加透明度（CSS 变量取到的是实色十六进制）；不可解析原样返回 */
export function withAlpha(color: string, alpha: number): string {
  const hex = color.trim();
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex);
  if (!m) return color;
  const h = m[1];
  const [r, g, b] = h.length === 3
    ? [h[0] + h[0], h[1] + h[1], h[2] + h[2]].map((x) => parseInt(x, 16))
    : [h.slice(0, 2), h.slice(2, 4), h.slice(4, 6)].map((x) => parseInt(x, 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function hasCoords(p: GlobePointData | null | undefined): p is GlobePointData {
  return !!p && Number.isFinite(p.lat) && Number.isFinite(p.lng);
}

/** 节点经纬度（geo 未解析/缺坐标均为 null） */
function nodeCoords(node: GlobeNode): GlobePointData | null {
  const lat = node.geo?.lat;
  const lng = node.geo?.lng;
  if (typeof lat !== 'number' || typeof lng !== 'number') return null;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return { lat, lng };
}

function nodeColor(node: GlobeNode, palette: GlobePalette): string {
  if (node.locked) return palette.locked;
  if (node.consecutive_fails > 0) return palette.failed;
  return palette.proxy;
}

function locationText(node: GlobeNode): string {
  const geo = node.geo;
  if (!geo) return '归属未知';
  const parts = [geo.country, geo.province, geo.city].filter((s): s is string => !!s);
  return parts.map(escapeHtml).join(' · ') || '归属未知';
}

/** 代理节点 tooltip：地址 + 归属 + 延迟 + 聚合速率 */
function nodeLabel(node: GlobeNode): string {
  return (
    `<strong>${escapeHtml(node.bind)}</strong><br/>` +
    `${locationText(node)}<br/>` +
    `延迟 ${fmtLatency(node.latency)}<br/>` +
    `↑ ${fmtBytesSpaced(node.up_rate)}/s ↓ ${fmtBytesSpaced(node.down_rate)}/s`
  );
}

/** 目标弧 tooltip：城市 + hosts + 聚合速率 */
function flowLabel(flow: GlobeFlow): string {
  const parts = [flow.country, flow.city].filter((s): s is string => !!s);
  const name = parts.map(escapeHtml).join(' · ') || '未知地区';
  const hosts = flow.hosts.map(escapeHtml).join('<br/>');
  return (
    `<strong>${name}</strong><br/>` +
    `${hosts}<br/>` +
    `↑ ${fmtBytesSpaced(flow.up_rate)}/s ↓ ${fmtBytesSpaced(flow.down_rate)}/s`
  );
}

/** 地球亮点：[本机（若配置）] + 有坐标的代理节点；无坐标节点跳过（不产 NaN） */
export function buildPoints(data: GlobeData, palette: GlobePalette): GlobePoint[] {
  const points: GlobePoint[] = [];
  if (hasCoords(data.self)) {
    points.push({
      lat: data.self.lat, lng: data.self.lng, color: palette.self,
      radius: 0.5, altitude: 0.015, label: '<strong>本机</strong>',
    });
  }
  for (const node of data.nodes) {
    const coords = nodeCoords(node);
    if (!coords) continue;
    points.push({
      ...coords, color: nodeColor(node, palette),
      radius: 0.35, altitude: 0.01, label: nodeLabel(node),
    });
  }
  return points;
}

/** 第一段弧：本机 → 每个有坐标的代理节点；本机坐标未配置时整层隐藏（返回空） */
export function buildProxyArcs(data: GlobeData, palette: GlobePalette): GlobeArc[] {
  const self = data.self;
  if (!hasCoords(self)) return [];
  const targets = data.nodes
    .map((node) => ({ node, coords: nodeCoords(node) }))
    .filter((x): x is { node: GlobeNode; coords: GlobePointData } => x.coords !== null);
  if (!targets.length) return [];
  const maxRate = Math.max(...targets.map(({ node }) => node.up_rate + node.down_rate));
  return targets.map(({ node, coords }) => ({
    startLat: self.lat,
    startLng: self.lng,
    endLat: coords.lat,
    endLng: coords.lng,
    color: withAlpha(palette.proxy, 0.55),
    stroke: rateToStroke(node.up_rate + node.down_rate, maxRate),
    dashTime: rateToDashTime(node.up_inst + node.down_inst),
    label: nodeLabel(node),
  }));
}

/** 第二段弧：代理节点 → 目标城市；bind 不在节点集（代理已离队/无坐标）或目标无坐标不产弧 */
export function buildTargetArcs(data: GlobeData, palette: GlobePalette): GlobeArc[] {
  const byBind = new Map<string, GlobePointData>();
  for (const node of data.nodes) {
    const coords = nodeCoords(node);
    if (coords) byBind.set(node.bind, coords);
  }
  // 先筛出真正会画的弧，再做速率归一：否则被跳过的 flow 会把画面最大值抬虚
  const drawn = data.flows
    .map((flow) => ({ flow, start: byBind.get(flow.bind) }))
    .filter((x): x is { flow: GlobeFlow; start: GlobePointData } => !!x.start)
    .filter(({ flow }) => hasCoords({ lat: flow.lat, lng: flow.lng }));
  if (!drawn.length) return [];
  const maxRate = Math.max(...drawn.map(({ flow }) => flow.up_rate + flow.down_rate));
  return drawn.map(({ flow, start }) => ({
    startLat: start.lat,
    startLng: start.lng,
    endLat: flow.lat,
    endLng: flow.lng,
    color: withAlpha(palette.target, 0.5),
    stroke: rateToStroke(flow.up_rate + flow.down_rate, maxRate),
    dashTime: rateToDashTime(flow.up_inst + flow.down_inst),
    label: flowLabel(flow),
  }));
}
