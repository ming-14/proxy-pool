/**
 * 队列代理归属地（bind → 国家/省/市）。
 *
 * 归属变化以月计，按 GEO_REFRESH_MS 节流（由页面 2s 节拍调用，不另起定时器）：
 * 归属地不进 /api/status 的 2s 热路径，也不想为它单开一个轮询。
 * geoip 关闭时后端不注册该路由，按 status.geo_enabled 跳过，不产生 404 请求。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import { pruneMap } from '@/utils/prune';
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
    // 保留集非空才清理：首帧 binds 必为空（displayStandby 尚未填充），误清一次要空窗到下一拍
    if (binds.length) pruneStandbyGeo(binds);
  } catch (err) {
    lastFetch = 0;
    console.warn('standby geo refresh failed', err);
  }
}

/** 丢弃已离队代理的归属地（binds 须非空：空集代表"队列暂无数据"，不等于都已离队） */
export function pruneStandbyGeo(binds: string[]): void {
  pruneMap(standbyGeo, binds);
}

/** 展示用归属地文本：国有/省市缺失时逐级退化（无归属返回 null） */
export function geoText(geo: GeoInfo | undefined): string | null {
  if (!geo) return null;
  return [geo.country, geo.province, geo.city].filter(Boolean).join(' · ');
}
