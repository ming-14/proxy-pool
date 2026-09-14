/**
 * 纯展示格式化函数（无 DOM 依赖，Vitest 直测）。
 * 输出格式与旧前端 utils 完全一致，保证视觉不变。
 */

/** 字节自适应：B / KB / MB / GB（保留旧两处实现的各自格式） */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '-';
  if (n < 1024) return `${n}B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)}KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)}MB`;
  return `${(n / 1073741824).toFixed(2)}GB`;
}

/** 流量 KPI 用：带空格、B 取整（与旧 dashboard.fmtBytes 一致） */
export function fmtBytesSpaced(n: number | null | undefined): string {
  if (n == null) return '-';
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '-';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

/** 粗粒度时长（秒）→ 文本：入队时长这类"看了知道大概多久"的场景，秒级粒度是噪声 */
export function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return '-';
  if (seconds < 60) return '不到 1 分钟';
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}

export function fmtLatency(value: number | null | undefined): string {
  return value == null ? '-' : `${value.toFixed(3)}s`;
}

/** 代理串 → bind（去掉 scheme 前缀）：http://1.2.3.4:8080 → 1.2.3.4:8080 */
export function bindOf(proxy: string): string {
  return proxy.includes('://') ? proxy.split('://')[1] : proxy;
}

/** 采样时间戳（秒）→ HH:MM:SS（zh-CN 24h 制，与旧图表 x 轴一致） */
export function fmtTime(tsSeconds: number): string {
  return new Date(tsSeconds * 1000).toLocaleTimeString('zh-CN', { hour12: false });
}

/** 加入时间戳（秒）→ 本地日期时间（zh-CN，与旧组表格一致） */
export function fmtDateTime(tsSeconds: number): string {
  return new Date((tsSeconds || 0) * 1000).toLocaleString('zh-CN');
}

/** HTML 转义：ECharts tooltip formatter 返回 HTML 字符串（不经 Vue 模板转义的路径） */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * 国家/地区显示名映射（仅展示层，不改数据）：
 * 中文名来自 GeoLite2（中国），英文名来自 world.json 区域（China）
 */
const COUNTRY_DISPLAY_MAP: Record<string, string> = {
  中国: '中国大陆',
  China: "China's Mainland",
};

export function displayCountryName(name: string): string {
  return COUNTRY_DISPLAY_MAP[name] ?? name;
}
