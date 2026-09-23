/**
 * 日志面板契约：组件不假设日志结构，宿主通过 source 注入数据、rules 提供取值方式。
 *
 * LogPanelEntry 是默认访问器假设的条目形状；宿主结构不同时覆盖对应 rules 访问器即可。
 */

/** 默认条目形状（DEFAULT_RULES 按此取值） */
export interface LogPanelEntry {
  ts: number;
  tsStr?: string;
  level: number;
  levelName?: string;
  tag?: string;
  text?: string;
  stack?: string | null;
}

/** 条目稳定标识：虚拟滚动测量缓存、展开态以它为键，必须跨过滤与环形淘汰保持稳定 */
export type LogPanelKey = string | number;

/** 源清空信号：宿主清空缓冲后推送，组件据此丢弃展开态并重读快照 */
export interface LogPanelClear {
  type: 'clear';
}

/** 日志源：宿主自己的缓冲（环形/数组皆可），组件只订阅增量、按帧读快照 */
export interface LogPanelSource<E> {
  /** 订阅新条目或清空信号，返回取消订阅函数 */
  subscribe(handler: (item: E | LogPanelClear) => void): () => void;
  /** 当前缓冲快照；宿主每次应返回新数组（同一数组原地修改不会触发重渲染） */
  getEntries(): E[];
  /** 清空缓冲，并须向订阅者推送清空信号（组件据此丢弃展开态、重读快照） */
  clear?(): void;
  getSize?(): number;
  getCapacity?(): number;
}

/** 取值访问器：从任意 entry 取出视窗所需元数据 */
export interface LogPanelRules<E> {
  /** 等级 0=DEBUG 1=INFO 2=WARN 3=ERROR */
  level(entry: E): number;
  levelName(entry: E): string;
  tag(entry: E): string;
  text(entry: E): string;
  /** 时间展示字符串 */
  tsStr(entry: E): string;
  /** 展开详情正文，无则 null */
  stack(entry: E): string | null;
  /** 是否可展开（有 stack 或等级 >= WARN） */
  isExpandable(entry: E): boolean;
  /** tag 配色表：命中用表内颜色，未命中按 tag 哈希出稳定色 */
  tagColor: Record<string, string>;
  /** 条目稳定标识；未提供时按对象引用标识（对象被重建会让行高缓存失效） */
  key?(entry: E): LogPanelKey;
}

/** 管线钩子：变换或丢弃条目、改写展示文本（搜索与复制导出共用同一文本） */
export interface LogPanelHooks<E> {
  onEntry?(entry: E): E | null | undefined;
  formatText?(text: string, entry: E): string;
}

/**
 * 功能开关：关掉即不渲染对应控件（拖拽/缩放/右键菜单等交互一并停用）。
 *
 * 形态相关的语义：`resize` 在抽屉形态下把关「档位切换」（点按/上下滑），关掉则固定半屏且不渲染抓手；
 * `drag` 只对悬浮形态有意义（抽屉没有自由定位），抽屉下惰性；`contextMenu` 两种形态都生效
 * （触屏经长按打开，见 parts/TitleBar.vue）。
 */
export interface LogPanelFeatures {
  drag: boolean;
  resize: boolean;
  export: boolean;
  copy: boolean;
  clear: boolean;
  search: boolean;
  regex: boolean;
  pause: boolean;
  autoScroll: boolean;
  tagFilter: boolean;
  levelFilter: boolean;
  stats: boolean;
  contextMenu: boolean;
}

/** 视窗几何（px，视口坐标） */
export interface LogPanelGeometry {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 视窗形态：auto 按视口宽度判定，floating 悬浮窗（桌面），sheet 底部抽屉（窄屏/触屏） */
export type LogPanelForm = 'auto' | 'floating' | 'sheet';

/** 抽屉档位：half 半屏（可视视口比例），full 全屏 */
export type LogPanelDetent = 'half' | 'full';

export type LogPanelTheme = 'auto' | 'light' | 'dark';

export type LogPanelWindowSize = 'small' | 'medium' | 'large' | 'xlarge';

export type LogPanelTranslator = (key: string, params?: Record<string, string | number>) => string;

/** 搜索高亮分段：hit 段渲染为 mark */
export interface HighlightSegment {
  text: string;
  hit: boolean;
}