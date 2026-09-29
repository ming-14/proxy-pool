/**
 * 队列代理归属地（bind → 国家/省/市）。
 *
 * 归属变化以月计，按 GEO_REFRESH_MS 节流（由页面 2s 节拍调用，不另起定时器）：
 * 归属地不进 /api/status 的 2s 热路径，也不想为它单开一个轮询。
 * geoip 关闭时后端不注册该路由，按 status.geo_enabled 跳过，不产生 404 请求。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import type { GeoInfo } from '@/types/api';

export const GEO_REFRESH_MS = 5 * 60 * 1000;

export const standbyGeo = reactive(new Map<string, GeoInfo>());

let lastFetch = 0;

/** 节流刷新归属地（首次调用立即拉取；失败重置节流窗口，下一拍重试） */
export async function refreshStandbyGeo(enabled: boolean, binds: string[]): Promise<void> {
  if (!enabled) return;
  const now = Date.now();
  if (now - lastFetch < GEO_REFRESH_MS) return;
  lastFetch = now;
  try {
    const data = await api.standbyGeo();
    for (const [bind, geo] of Object.entries(data.geo)) {
      if (geo) standbyGeo.set(bind, geo);
    }
    // 只在保留集非空时清理：本函数有 5 分钟节流，误清一次就要空窗到下一拍才恢复。
    // 进入页面首帧必然带空 binds（displayStandby 尚未填充），若照常清理会清空整个
    // 归属地缓存，此后 5 分钟内所有行的归属地列都是空的。
    if (binds.length) pruneStandbyGeo(binds);
  } catch (err) {
    lastFetch = 0;
    console.warn('standby geo refresh failed', err);
  }
}

/**
 * 丢弃已离队代理的归属地。
 *
 * 归属地按 bind 累积，代理离队后条目永不再命中，不清理会随驻留时长无界增长
 * （与 useStandbySeries 的 pruneBuffers 同一口径：由页面按当前队列传入保留集）。
 * 须由调用方保证 binds 非空——空集代表"队列暂无数据"，不等于"代理都已离队"。
 */
export function pruneStandbyGeo(binds: string[]): void {
  const keep = new Set(binds);
  for (const bind of standbyGeo.keys()) {
    if (!keep.has(bind)) standbyGeo.delete(bind);
  }
}

/** 展示用归属地文本：国有/省市缺失时逐级退化（无归属返回 null） */
export function geoText(geo: GeoInfo | undefined): string | null {
  if (!geo) return null;
  return [geo.country, geo.province, geo.city].filter(Boolean).join(' · ');
}
