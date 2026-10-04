/**
 * 后端 /api/* DTO 类型：与 src/framework/web/api.py 的响应结构一一对应。
 * 契约变更（新增/删字段）必须同步此文件。
 */

export interface GroupStat {
  ttl: number | null;      // null = 永不过期
  count: number;
  active: number;
  label: string;           // 中文显示名（后端由 config 派生）
  kind: 'trusted' | 'blacklist' | 'standby' | 'staging' | 'rejected' | 'normal';
}

/** 面板日志条目（GET /api/logs），与后端 logbuffer.py 的 entry 结构一致 */
export interface LogEntry {
  seq: number;             // 单调递增游标（增量拉取位置）
  ts: number;              // 毫秒时间戳
  level: number;           // 0=DEBUG 1=INFO 2=WARN 3=ERROR
  levelName: string;
  tag: string;             // logger 名（如 src.use_cases.fetch_proxies）
  text: string;
  stack?: string | null;   // 异常堆栈（可展开详情，无异常为 null）
}

/** GET /api/logs?since=N */
export interface LogsResponse {
  entries: LogEntry[];
  cursor: number;          // 当前最新 seq（后续增量拉取起点）
  buffer_start: number;    // 缓冲最旧条目 seq（serve 重启/淘汰后增量失效检测）
}

export interface StandbyEntry {
  proxy: string;
  latency: number | null;
  consecutive_fails: number;
  pinned: boolean;
  locked: boolean;             // 锁定为 pproxy 唯一上游（列表置顶显示，其余行灰显）
  purity: PurityInfo | null;   // 缓存的纯净度（无缓存为 null）
  joined_at: number | null;    // 进入队列组的时间（秒）；锁定行的值即锁定时刻
  speedtest?: SpeedTestStatus | null;  // 后端测速任务状态（刷新后恢复；无记录为 undefined/null）
  purity_state?: PurityState | null;   // 后端纯净度检查中间态（checking/error；无记录为 undefined/null）
}

/** 单代理序列点：[时间戳（秒）, 值]；延时序列的值为 null 表示该轮探测失败 */
export type SeriesPoint = [number, number | null];

/** GET /api/standby/series?since=<ts> 的逐代理序列（缺字段表示该代理无此序列） */
export interface StandbySeriesEntry {
  lat?: SeriesPoint[];                // 延时采样（失败点值为 null，前端断线渲染）
  up?: [number, number][];            // 累计上行字节（前端差分算速率）
  down?: [number, number][];          // 累计下行字节
}

export interface StandbySeries {
  cursor: number;                     // 本次下发点的最大时间戳（下轮 since）
  window: number;                     // 队列页图表的显示窗口（秒，= chart_window）：前端据此裁剪本地缓冲（仅队列页）
  series: Record<string, StandbySeriesEntry>;  // key 为 bind（地址，不含 scheme）
}

/** GET /api/standby/geo：bind → 归属地（未解析出为 null） */
export interface StandbyGeoMap {
  geo: Record<string, GeoInfo | null>;
}

/** IP 用途分类（核心侧通用枚举；各检查源的原值由插件映射到这些值） */
export type UsageType =
  | 'residential'
  | 'mobile'
  | 'hosting'
  | 'business'
  | 'education'
  | 'government'
  | 'military'
  | 'library'
  | 'cdn'
  | 'search_engine'
  | 'reserved'
  | 'unknown';

export interface PurityInfo {
  score?: number | null;               // 0-100，越高越脏（None = 源未给出）
  usage_type?: UsageType | string | null;
  // /api/proxy/purity 完整响应另含 ip/extra 等，tag 仅用以上两字段
  [key: string]: unknown;
}

/** 纯净度检查源元数据（null = 功能未启用：web 关闭 / 未选源 / 缺密钥） */
export interface PuritySource {
  name: string;
  label: string;
}

export interface SourceStat {
  name: string;            // 完整模块路径（暂停记录 key）
  short_name: string;
  enabled: boolean;
  paused: boolean;
  pause_remaining: number | null;
  fetching: boolean;       // 该源正在拉取（全量拉取时为 true；后端状态，刷新后仍可恢复）
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

/** 入队筛选规则（settings.enqueue_rules；优先级来自配置，只读） */
export interface EnqueueRuleSetting {
  metric: string;                        // latency | country | purity | speed
  label: string;                         // 中文展示名（后端由指标派生）
  priority: number;                      // 升序执行，数字小先测
  enabled: boolean;
  params: Record<string, number | string[]>;  // 阈值：max_latency / allow / max_score / min_download_mbps
}

/** POST /api/settings 的入队规则补丁：按指标更新启停与阈值 */
export interface EnqueueRulePatch {
  metric: string;
  enabled: boolean;
  max_latency?: number;
  allow?: string[];
  max_score?: number;
  min_download_mbps?: number;
}

/** 运行时可调项快照：调度参数 + 入队筛选规则 */
export interface SettingsSnapshot extends ScheduleSettings {
  enqueue_rules: EnqueueRuleSetting[];
}

/** POST /api/settings 补丁：调度参数子集 + 入队规则 */
export interface SettingsPatch extends Partial<ScheduleSettings> {
  enqueue_rules?: EnqueueRulePatch[];
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
  globe_enabled: boolean;  // 3D 地球可用（geoip + [globe].enabled 均开启）；前端据此隐藏地球卡片
  purity_source: PuritySource | null;  // 纯净度检查源（前端据此隐藏检查按钮并标注来源）
  standby: StandbyEntry[];
  standby_count: number;
  locked_proxy: string | null;
  capacity: number;
  pending_retries: string[];
  fetching_all: boolean;   // 全量拉取进行中（后端状态，刷新后仍可恢复）
  sources: SourceStat[];
  settings: SettingsSnapshot;
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
  retention_window: number;           // 服务端留存时长（秒，= METRICS_WINDOW）：前端本地缓冲据此按时间裁剪
  traffic_total: TrafficTotal;
}

export interface GeoInfo {
  country: string;
  country_code: string;
  province: string | null;
  city: string | null;
}

/** GET /api/globe 代理节点归属（含经纬度，地球亮点定位用；未解析出为 null） */
export interface GlobeGeo {
  country: string;
  country_code: string;
  province: string | null;
  city: string | null;
  lat: number | null;
  lng: number | null;
}

/** GET /api/globe 代理节点（地球亮点；速率取自 PproxyServer 的 pair 聚合） */
export interface GlobeNode {
  bind: string;
  geo: GlobeGeo | null;
  latency: number | null;
  consecutive_fails: number;
  pinned: boolean;
  locked: boolean;
  up_rate: number;   // 聚合 EMA 速率（B/s，弧线宽）
  down_rate: number;
  up_inst: number;   // 瞬时速率（B/s，光点流速）
  down_inst: number;
}

/** GET /api/globe 目标弧（代理 → 目标城市；lat/lng 必有，无坐标的行不上弧） */
export interface GlobeFlow {
  bind: string;
  country: string | null;
  country_code: string | null;
  city: string | null;
  lat: number;
  lng: number;
  up_rate: number;
  down_rate: number;
  up_inst: number;
  down_inst: number;
  hosts: string[];
}

/** GET /api/globe（globe_enabled=false 时该路由不存在） */
export interface GlobeData {
  self: { lat: number; lng: number } | null;  // panel.toml [globe] 手填坐标；null=未配置
  nodes: GlobeNode[];
  flows: GlobeFlow[];
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

/** 后端任务状态：测速（/api/status standby 行 + POST /api/proxy/speedtest 响应） */
export interface SpeedTestStatus {
  status: 'running' | 'done' | 'error';
  result?: SpeedTestResult | null;
  error?: string | null;
  ts?: number | null;   // 完成时间戳（秒，前端格式化显示"什么时候测的"）
}

/** 后端任务状态：纯净度检查中间态（/api/status standby 行；结果由 SQLite 缓存承载） */
export interface PurityState {
  status: 'checking' | 'error';
  error?: string | null;
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
  settings: SettingsSnapshot;
}
