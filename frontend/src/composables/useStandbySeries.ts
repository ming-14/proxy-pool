/**
 * 队列逐代理序列缓冲（行内趋势图的数据源）。
 *
 * 与 /api/status 同节拍（2s）由页面驱动增量拉取（携带游标），本地按指标窗口裁剪：
 * 交出去的是「bind → 延时/流量原始序列」，页面对其降采样后渲染。
 *
 * 模块级状态：跨路由保留，回到队列页不重新拉全量（游标续接）；离队代理的缓冲由
 * pruneBuffers 丢弃，长时间驻留不会无界增长。
 */
import { reactive, ref } from 'vue';
import { api } from '@/api/client';
import { pruneMap } from '@/utils/prune';
import { dedupeByTs, trimBefore } from '@/utils/series';
import type { SeriesPoint, StandbySeriesEntry } from '@/types/api';

/**
 * 本地缓冲保留时长（秒）：由服务端 `/api/standby/series` 的 `window` 下发（即后端
 * chart_window，代理服务队列页图表的显示窗口，仅服务队列页行内图）。不在前端
 * 硬编码——配置一改两边窗口就会各自漂移，显示的时间跨度对不上。
 * 注意：此处裁剪只作用于队列页；仪表盘走 /api/status/history，按真实数据点绘制，不裁剪。
 * null = 尚无响应，此时不裁剪（宁可多留一拍，也不误删未对齐窗口的点）。
 */
let windowS: number | null = null;

export interface SeriesBuffer {
  lat: SeriesPoint[];
  up: [number, number][];
  down: [number, number][];
}

export const seriesBuffers = reactive(new Map<string, SeriesBuffer>());

/** 增量游标：已合并点的最大时间戳（服务端保证时间序） */
const cursor = ref(0);

function mergeEntry(buf: SeriesBuffer, entry: StandbySeriesEntry): void {
  if (entry.lat) buf.lat = dedupeByTs(buf.lat.concat(entry.lat));
  if (entry.up) buf.up = dedupeByTs(buf.up.concat(entry.up)) as [number, number][];
  if (entry.down) buf.down = dedupeByTs(buf.down.concat(entry.down)) as [number, number][];
}

/** 丢弃离队代理的缓冲 + 裁剪窗口外的点；缓冲一旦全空，游标归零（下次必须全量拉取） */
function pruneBuffers(binds: string[]): void {
  pruneMap(seriesBuffers, binds);
  if (windowS !== null) {
    const cutoff = Date.now() / 1000 - windowS;
    for (const buf of seriesBuffers.values()) {
      buf.lat = trimBefore(buf.lat, cutoff);
      buf.up = trimBefore(buf.up, cutoff);
      buf.down = trimBefore(buf.down, cutoff);
    }
  }
  // 队列清空后再填充时，保留旧游标会只拿到增量点、图表从空白开始
  if (seriesBuffers.size === 0) cursor.value = 0;
}

/** 拉取增量并合并（失败静默：下一轮 2s 节拍自动重试，不打断状态轮询） */
export async function refreshStandbySeries(binds: string[]): Promise<void> {
  try {
    const data = await api.standbySeries(cursor.value || undefined);
    cursor.value = data.cursor || cursor.value;
    windowS = data.window;
    for (const [bind, entry] of Object.entries(data.series)) {
      const buf = seriesBuffers.get(bind) ?? { lat: [], up: [], down: [] };
      mergeEntry(buf, entry);
      seriesBuffers.set(bind, buf);
    }
  } catch (err) {
    console.warn('standby series refresh failed', err);
  }
  pruneBuffers(binds);
}
