/**
 * 后端 /api/* DTO 类型：与 src/framework/web/api.py 的响应结构一一对应。
 * 契约变更（新增/删字段）必须同步此文件。
 */

export interface GroupStat {
  ttl: number | null;      // null = 永不过期
  count: number;
  active: number;
  label: string;           // 中文显示名（后端由 config 派生）
  kind: 'trusted' | 'blacklist' | 'standby' | 'staging' | 'normal';
}

export interface StandbyEntry {
  proxy: string;
  latency: number | null;
  consecutive_fails: number;
  pinned: boolean;
  purity: PurityInfo | null;   // 缓存的纯洁度（无缓存为 null）
}

export interface PurityInfo {
  abuse_confidence_score?: number | null;
  usage_type?: string | null;
  // /api/proxy/purity 完整响应另含 ip/last_seen_days 等，tag 仅用以上两字段
  [key: string]: unknown;
}

export interface SourceStat {
  name: string;            // 完整模块路径（暂停记录 key）
  short_name: string;
  enabled: boolean;
  paused: boolean;
  pause_remaining: number | null;
}

export interface ScheduleSettings {
  interval: number;
  concurrency: number;
  timeout: number;
  probe_url: string;
  allowed_types: string[];
  fill_probe_multiplier: number;
  fill_probe_min_extra: number;
  salgorithm: 'fa' | 'rr' | 'rc' | 'lc';
}

export interface TrafficTotal {
  upload: number;
  download: number;
}

/** GET /api/status */
export interface StatusSnapshot {
  groups: Record<string, GroupStat>;
  available_count: number;
  geo_enabled: boolean;
  standby: StandbyEntry[];
  standby_count: number;
  locked_proxy: string | null;
  locked_purity: PurityInfo | null;
  capacity: number;
  pending_retries: string[];
  sources: SourceStat[];
  settings: ScheduleSettings;
  traffic_total: TrafficTotal;
}

/** /api/status/history 采样点（standby 队列指标） */
export interface StandbyHistoryPoint {
  ts: number;
  avg: number | null;
  min: number | null;
  max: number | null;
  health_rate: number | null;
  queue_length: number;
  locked: boolean;
}

/** /api/status/history 采样点（流量累计值） */
export interface TrafficHistoryPoint {
  ts: number;
  total_upload: number;
  total_download: number;
}

/** GET /api/status/history?since=<ts> */
export interface StatusHistory {
  standby_history: StandbyHistoryPoint[];
  traffic_history: TrafficHistoryPoint[];
  traffic_total: TrafficTotal;
}

export interface GeoInfo {
  country: string;
  country_code: string;
  province: string | null;
  city: string | null;
}

export interface GroupProxyEntry {
  proxy: string;
  joined_at: number;
  consecutive_fails: number;
  latency: number | null;
  expires_in: number | null;
  source_group: string | null;
  geo: GeoInfo | null;
}

/** GET /api/group/{name} */
export interface GroupDetail {
  group: string;
  ttl: number | null;
  count: number;
  proxies: GroupProxyEntry[];
}

/** GET /api/proxy/history?bind= */
export interface ProxyHistory {
  bind: string;
  latency_history: { ts: number; latency: number | null }[];
  traffic_history: TrafficHistoryPoint[];
}

/** POST /api/proxy/speedtest */
export interface SpeedLeg {
  ok: boolean | null;
  best_mbps?: number | null;
  avg_mbps?: number | null;
  bytes?: number | null;
  ms?: number | null;
  node?: string | null;
  err?: string | null;
}

export interface SpeedTestResult {
  proxy: string;
  download: SpeedLeg | null;
  upload: SpeedLeg | null;
}

/** GET /api/geo/map */
export interface GeoMapPoint {
  name: string;
  lng: number;
  lat: number;
  count: number;
  country: string;
}

export interface GeoMapData {
  total_ips: number;
  resolved_ips: number;
  unresolved_ips: number;
  countries: { name: string; count: number }[];
  points: GeoMapPoint[];
}

/** POST /api/settings 响应 */
export interface SettingsResponse {
  ok: boolean;
  changed: Record<string, unknown>;
  settings: ScheduleSettings;
}
