/**
 * LogPanel 类型声明（vendored 零依赖组件，源码为 JS，此处补充 TS 接口）。
 * 与上游 logpanel 的 API 契约保持一致。
 */

/** 面板日志条目（后端 /api/logs 返回结构） */
export interface LogPanelEntry {
  seq: number;
  ts: number;
  level: number; // 0=DEBUG 1=INFO 2=WARN 3=ERROR
  levelName: string;
  tag: string;
  text: string;
  stack?: string | null;
  hasStack?: boolean;
}

/** 日志源接口：LogPanel 通过它获取/订阅日志 */
export interface LogPanelSource {
  subscribe(cb: (entry: LogPanelEntry | { type: 'clear' }) => void): () => void;
  getEntries(): LogPanelEntry[];
  clear(): void;
  getSize(): number;
  getCapacity(): number;
}

/** 解析规则访问器：从 entry 提取视窗所需元数据 */
export interface LogPanelRules {
  level(e: LogPanelEntry): number;
  levelName(e: LogPanelEntry): string;
  tag(e: LogPanelEntry): string;
  text(e: LogPanelEntry): string;
  ts(e: LogPanelEntry): number;
  tsStr(e: LogPanelEntry): string;
  stack(e: LogPanelEntry): string | null;
  isExpandable(e: LogPanelEntry): boolean;
  tagColor: Record<string, string>;
}

/** 钩子：管线变换能力 */
export interface LogPanelHooks {
  onEntry?(entry: LogPanelEntry): LogPanelEntry | null | undefined;
  formatText?(text: string, entry: LogPanelEntry): string;
}

export interface LogPanelOptions {
  source: LogPanelSource;
  rules?: Partial<LogPanelRules>;
  hooks?: LogPanelHooks;
  storageKeyPrefix?: string;
  theme?: 'auto' | 'light' | 'dark';
  mountEl?: HTMLElement;
  initialGeometry?: { x: number; y: number; w: number; h: number; zoom: number };
  initialVisible?: boolean;
  windowSize?: 'small' | 'medium' | 'large' | 'xlarge';
  windowOpacity?: number;
  features?: Partial<Record<'drag' | 'resize' | 'export' | 'copy' | 'clear' | 'search' | 'regex' | 'pause' | 'autoScroll' | 'tagFilter' | 'levelFilter' | 'stats' | 'contextMenu' | 'windowSize' | 'opacity', boolean>>;
  title?: string;
  t?(key: string, params?: Record<string, string>): string;
}

export class LogPanel {
  constructor(opts: LogPanelOptions);
  show(): void;
  hide(): void;
  destroy(): void;
  isVisible(): boolean;
  setOption(key: 'theme', value: 'auto' | 'light' | 'dark'): void;
  setOption(key: 'windowSize', value: 'small' | 'medium' | 'large' | 'xlarge'): void;
  setOption(key: 'windowOpacity', value: number): void;
  setOption(key: 'rules', value: Partial<LogPanelRules>): void;
  setOption(key: 'hooks', value: LogPanelHooks): void;
  on(event: 'visibleChange', cb: (visible: boolean) => void): () => void;
  on(event: 'destroy', cb: () => void): () => void;
}

export const DEFAULT_RULES: LogPanelRules;
export function resolveTagColor(tag: string, tagColorTable: Record<string, string>): string;