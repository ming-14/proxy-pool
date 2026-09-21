/**
 * LogPanel 通用悬浮日志视窗（Vue 3 + TypeScript，样式隔离用 scoped CSS + --lp-* 变量）。
 *
 * 用法：
 *   import { LogPanel, DEFAULT_RULES } from '@/vendor/logpanel';
 *   <LogPanel v-model:visible="shown" :source="source" :rules="{ ...DEFAULT_RULES, key: (e) => e.id }" />
 */
export { default as LogPanel } from './LogPanel.vue';
export { DEFAULT_RULES, DEFAULT_TAG_COLORS } from './defaults';
export type {
  HighlightSegment,
  LogPanelClear,
  LogPanelEntry,
  LogPanelFeatures,
  LogPanelGeometry,
  LogPanelHooks,
  LogPanelKey,
  LogPanelRules,
  LogPanelSource,
  LogPanelTheme,
  LogPanelTranslator,
  LogPanelWindowSize,
} from './types';