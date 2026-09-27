/**
 * 类型化 API 客户端：统一 fetch、JSON 解析与错误抛出。
 * 后端非 2xx 响应约定为 {"error": "..."}，包装为 ApiError 供调用方展示。
 */
import type {
  GeoMapData,
  GroupDetail,
  LogsResponse,
  ScheduleSettings,
  SpeedTestResult,
  StandbyGeoMap,
  StandbySeries,
  StatusHistory,
  StatusSnapshot,
} from '@/types/api';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const msg = (data as { error?: string } | null)?.error ?? `HTTP ${response.status}`;
    throw new ApiError(msg, response.status);
  }
  return data as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export const api = {
  status: () => request<StatusSnapshot>('/api/status'),

  statusHistory: (since?: number) =>
    request<StatusHistory>(`/api/status/history${since ? `?since=${encodeURIComponent(since)}` : ''}`),

  /** 队列逐代理序列（行内趋势图）：缺省全量，since 为增量游标 */
  standbySeries: (since?: number) =>
    request<StandbySeries>(`/api/standby/series${since ? `?since=${encodeURIComponent(since)}` : ''}`),

  /** 队列代理归属地（bind → geo，5 分钟节拍拉取；geoip 关闭时该路由不存在） */
  standbyGeo: () => request<StandbyGeoMap>('/api/standby/geo'),

  group: (name: string) => request<GroupDetail>(`/api/group/${encodeURIComponent(name)}`),

  addProxy: (group: string, proxies: string[]) =>
    post<{ ok: boolean; added: number }>(`/api/group/${encodeURIComponent(group)}/add`, { proxies }),

  /** 拉取代理；source 非空时只拉该源。后端已有拉取在跑时返回 {"status":"running"}（幂等拒绝） */
  fetch: (source?: string) =>
    post<{ ok: boolean; available: number } | { status: 'running' }>('/api/fetch', { source }),

  blacklist: (proxies: string[]) => post<{ ok: boolean; count: number }>('/api/blacklist', { proxies }),

  pauseSource: (source: string, ttl?: number) =>
    post<{ ok: boolean; pause_remaining: number | null }>('/api/source/pause', { source, ttl }),

  resumeSource: (source: string) => post<{ ok: boolean; resumed: boolean }>('/api/source/resume', { source }),

  pin: (proxy: string) => post<{ ok: boolean }>('/api/standby/pin', { proxy }),

  unpin: (proxy: string) => post<{ ok: boolean }>('/api/standby/unpin', { proxy }),

  lock: (proxy: string) => post<{ ok: boolean }>('/api/standby/lock', { proxy }),

  unlock: () => post<{ ok: boolean; proxy: string | null }>('/api/standby/unlock', {}),

  settings: (patch: Partial<ScheduleSettings>) =>
    post<{ ok: boolean; changed: Record<string, unknown>; settings: ScheduleSettings }>('/api/settings', patch),

  /** 测速：进行中（幂等）时后端返回 {"status":"running"} */
  speedTest: (bind: string) => post<SpeedTestResult | { status: 'running' }>('/api/proxy/speedtest', { bind }),

  purity: (bind: string) => post<Record<string, unknown>>('/api/proxy/purity', { bind }),

  geoMap: () => request<GeoMapData>('/api/geo/map'),

  logs: (since?: number) =>
    request<LogsResponse>(`/api/logs${since ? `?since=${encodeURIComponent(since)}` : ''}`),

  clearLogs: () => post<{ ok: boolean }>('/api/logs/clear', {}),

  moveProxy: (proxies: string[], targetGroup: string) =>
    post<{ ok: boolean; count: number }>('/api/proxy/move', { proxies, target_group: targetGroup }),

  removeProxy: (proxies: string[], group: string) =>
    post<{ ok: boolean; count: number }>('/api/proxy/remove', { proxies, group }),
};
