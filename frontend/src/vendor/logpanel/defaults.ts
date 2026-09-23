/**
 * 组件常量与默认值：宿主未覆盖时的取值、内置中文文案、持久化键名。
 */
import type {
  LogPanelEntry,
  LogPanelFeatures,
  LogPanelRules,
  LogPanelTranslator,
  LogPanelWindowSize,
} from './types';
import { formatTime } from './utils/format';

/** 等级档位名，下标即等级（0=DEBUG … 3=ERROR） */
export const LEVEL_NAMES = ['DEBUG', 'INFO', 'WARN', 'ERROR'] as const;

/** 等级筛选 chip 的档位定义 */
export const LEVELS = LEVEL_NAMES.map((name, level) => ({ level, name }));

/** 尺寸预设（仅改窗口宽高，不改内容度量） */
export const WINDOW_SIZES: Record<LogPanelWindowSize, { w: number; h: number }> = {
  small: { w: 480, h: 320 },
  medium: { w: 560, h: 360 },
  large: { w: 720, h: 480 },
  xlarge: { w: 960, h: 600 },
};

/** 默认窗口尺寸与距视口边距（px） */
export const DEFAULT_WINDOW_W = 560;
export const DEFAULT_WINDOW_H = 360;
export const WINDOW_EDGE_GAP = 20;
/** 窗口最小尺寸与贴合视口时的留边（px） */
export const MIN_WINDOW_W = 320;
export const MIN_WINDOW_H = 200;
export const VIEWPORT_MARGIN = 8;

/** 抽屉形态判据：窄屏，或横屏手机（宽而矮）。宽而矮的桌面窗口仍用悬浮窗——那种视口下悬浮窗能挪、够宽 */
export const SHEET_MEDIA_QUERY = '(max-width: 640px), (max-width: 900px) and (max-height: 480px)';
/** 触控判据：主指针是手指。与形态正交——形态看空间，触控看指针类型（触屏笔记本仍用鼠标） */
export const TOUCH_MEDIA_QUERY = '(pointer: coarse)';

/** 抽屉档位：半屏占可视视口高度比例、最小高度、拖拽定档阈值（占档位行程比例）与快速滑动判定 */
export const SHEET_HALF_RATIO = 0.55;
export const MIN_SHEET_H = 240;
export const SHEET_DETENT_RATIO = 0.2;
export const SHEET_FLING_V = 0.8;
/** 快速滑动判定所需的最小位移（px）：更小的抖动按位移定档，不认速度 */
export const SHEET_FLING_MIN_DY = 40;

/** 标题栏手势：长按阈值与拖拽死区（px） */
export const LONG_PRESS_MS = 500;
export const DRAG_SLOP = 8;

export const DEFAULT_FEATURES: LogPanelFeatures = {
  drag: true,
  resize: true,
  export: true,
  copy: true,
  clear: true,
  search: true,
  regex: true,
  pause: true,
  autoScroll: true,
  tagFilter: true,
  levelFilter: true,
  stats: true,
  contextMenu: true,
};

/** 内置 tag 配色：仅登记默认 tag，其余按哈希着色（宿主可整体覆盖/追加） */
export const DEFAULT_TAG_COLORS: Record<string, string> = {
  default: '#888888',
};

/** 持久化键名（前缀由 storageKeyPrefix 提供），沿用旧版键名以免用户已存的几何/筛选丢失 */
export const STORAGE_KEYS = {
  geometry: 'geom',
  levels: 'level_filter',
  hiddenTags: 'tag_filter',
  follow: 'autoscroll',
  /** 抽屉档位与筛选条开合：与悬浮几何分键，互不污染（窄屏的尺寸不写进桌面几何） */
  detent: 'sheet_detent',
  sideOpen: 'side_open',
} as const;

/** 内置中文文案；宿主传 t 时整体替换（键名沿用旧版，便于复用既有翻译表） */
export const DEFAULT_TEXT: Record<string, string> = {
  'logpanel.title': '日志视窗',
  'logpanel.pauseReceiving': '暂停接收新日志',
  'logpanel.resumeReceiving': '继续接收新日志',
  'logpanel.pausedSuffix': ' · 暂停',
  'logpanel.enableAutoScroll': '开启自动滚动',
  'logpanel.disableAutoScroll': '关闭自动滚动',
  'logpanel.close': '关闭',
  'logpanel.searchPlaceholder': '搜索日志（支持正则）…',
  'logpanel.toggleRegex': '切换正则模式',
  'logpanel.clearLog': '清空日志缓冲区',
  'logpanel.clear': '清空',
  'logpanel.copyFilter': '复制当前过滤结果到剪贴板',
  'logpanel.copy': '复制',
  'logpanel.exportTxt': '导出为 txt 文件',
  'logpanel.export': '导出',
  'logpanel.moduleFilter': '模块筛选',
  'logpanel.selectAll': '全选',
  'logpanel.selectNone': '取消全选',
  'logpanel.expandSheet': '展开到全屏',
  'logpanel.collapseSheet': '收起到半屏',
  'logpanel.stats': '统计',
  'logpanel.logCleared': '日志已清空',
  'logpanel.copied': '已复制 {n} 条',
  'logpanel.copyFailed': '复制失败',
  'logpanel.exported': '已导出 {fname}',
  'logpanel.exportFailed': '导出失败: {err}',
  'logpanel.closePanel': '关闭日志视窗',
  'logpanel.bufferLabel': '缓冲 {used} / {cap} 条 {pct}%',
  'logpanel.bufferUsed': '缓冲 {used} 条',
};

/** 默认取值访问器：按 LogPanelEntry 形状取值，宿主结构不同时必须覆盖对应访问器 */
export const DEFAULT_RULES: Omit<LogPanelRules<LogPanelEntry>, 'key'> = {
  level: (entry) => entry.level,
  levelName: (entry) => entry.levelName || LEVEL_NAMES[entry.level] || '?',
  tag: (entry) => entry.tag || 'default',
  text: (entry) => entry.text || '',
  tsStr: (entry) => entry.tsStr || (typeof entry.ts === 'number' ? formatTime(entry.ts) : ''),
  stack: (entry) => entry.stack || null,
  isExpandable: (entry) => !!entry.stack || entry.level >= 2,
  tagColor: { ...DEFAULT_TAG_COLORS },
};

/**
 * 合并默认访问器与宿主覆盖。
 * 默认访问器按 LogPanelEntry 取值，E 为其它结构时由宿主覆盖；此处一次性收口该假设。
 */
export function resolveRules<E>(overrides?: Partial<LogPanelRules<E>>): LogPanelRules<E> {
  return {
    ...(DEFAULT_RULES as unknown as LogPanelRules<E>),
    ...overrides,
    tagColor: { ...DEFAULT_TAG_COLORS, ...overrides?.tagColor },
  };
}

/** 本地化函数：宿主未提供时用内置中文文案，缺失键原样返回 */
export function translator(t?: LogPanelTranslator): LogPanelTranslator {
  if (t) return t;
  return (key, params) => {
    let text = DEFAULT_TEXT[key] ?? key;
    if (params) {
      for (const [name, value] of Object.entries(params)) {
        text = text.split(`{${name}}`).join(String(value));
      }
    }
    return text;
  };
}