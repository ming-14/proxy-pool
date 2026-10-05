/**
 * 类型化 API 客户端：统一 fetch、JSON 解析与错误抛出。
 * 后端非 2xx 响应约定为 {"error": "..."}，包装为 ApiError 供调用方展示。
 */
import type {
  GeoMapData,
  GlobeData,
  GroupDetail,
  LogsResponse,
  SettingsPatch,
  SettingsResponse,
  SpeedTestResult,
  StandbyGeoMap,
  StandbySeries,
  StatusHistory,
  StatusSnapshot,
  TrafficHistoryResponse,
  TrafficLiveResponse,
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

/**
 * 把任意抛出物转成可展示文案。
 *
 * 全前端唯一的错误取文案入口：ApiError 已带后端下发的中文 message，其余 Error 取
 * message，非 Error（字符串/undefined）兜底 String()。调用方一律走这里，不各自
 * 手写三元表达式——否则同一件事在不同页面会漂成不同形态（接入前散落 21 处）。
 */
export function errText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * 请求超时（毫秒）。
 *
 * 面板按固定节拍轮询，若不设上限，后端挂起（TCP 连上但不回包）时请求会永远挂着、
 * 每轮再叠一个，失败也永远不触发重试路径。取值明显大于正常往返（本地/局域网毫秒级），
 * 只为兜住"后端僵死"这种病态场景。
 */
const REQUEST_TIMEOUT_MS = 15_000;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
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
    post<{ ok: boolean; added: number; rejected: string[] }>(
      `/api/group/${encodeURIComponent(group)}/add`, { proxies }),

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

  settings: (patch: SettingsPatch) => post<SettingsResponse>('/api/settings', patch),

  /** 测速：进行中（幂等）时后端返回 {"status":"running"} */
  speedTest: (bind: string) => post<SpeedTestResult | { status: 'running' }>('/api/proxy/speedtest', { bind }),

  purity: (bind: string) => post<Record<string, unknown>>('/api/proxy/purity', { bind }),

  geoMap: () => request<GeoMapData>('/api/geo/map'),

  /** 3D 地球数据（节点 + 目标弧）；每次请求即刷新后端探测激活窗口 */
  globe: () => request<GlobeData>('/api/globe'),

  /** 实时流量：此刻仍开着的连接（连接级口径，按「代理, 域名」汇总） */
  trafficLive: () => request<TrafficLiveResponse>('/api/traffic/live'),

  /** 历史流量条目（按流量总计降序，条数上限由后端配置决定） */
  trafficHistory: () => request<TrafficHistoryResponse>('/api/traffic/history'),

  logs: (since?: number) =>
    request<LogsResponse>(`/api/logs${since ? `?since=${encodeURIComponent(since)}` : ''}`),

  clearLogs: () => post<{ ok: boolean }>('/api/logs/clear', {}),

  moveProxy: (proxies: string[], targetGroup: string) =>
    post<{ ok: boolean; count: number }>('/api/proxy/move', { proxies, target_group: targetGroup }),

  removeProxy: (proxies: string[], group: string) =>
    post<{ ok: boolean; count: number }>('/api/proxy/remove', { proxies, group }),
};
