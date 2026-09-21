<script setup lang="ts" generic="E extends object">
/**
 * LogPanel：通用悬浮日志视窗。
 *
 * 数据与取值由宿主注入（source / rules / hooks），组件只负责视窗交互与渲染管线；
 * 样式经 scoped CSS + --lp-* 变量暴露——宿主在组件根或其任意祖先设置变量即可换肤，
 * 组件默认值写在 --lp-*-c 解析层，避免默认值屏蔽宿主传入的变量。
 *
 * source / rules / hooks 视为常量（组件不监听其身份变化），主题/尺寸/透明度随 props 响应。
 *
 * 用法：
 *   <LogPanel v-model:visible="shown" :source="source" :rules="rules" title="控制台日志" />
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { DEFAULT_FEATURES, STORAGE_KEYS, VIEWPORT_MARGIN, resolveRules, translator } from './defaults';
import type {
  LogPanelFeatures,
  LogPanelGeometry,
  LogPanelHooks,
  LogPanelRules,
  LogPanelSource,
  LogPanelTheme,
  LogPanelTranslator,
  LogPanelWindowSize,
} from './types';
import { useLogView } from './composables/useLogView';
import { useToasts } from './composables/useToasts';
import { useWindowGeometry } from './composables/useWindowGeometry';
import { copyText } from './utils/clipboard';
import { exportFileName } from './utils/format';
import { createStorage } from './utils/storage';
import SidePanel from './parts/SidePanel.vue';
import TitleBar from './parts/TitleBar.vue';
import ToastStack from './parts/ToastStack.vue';
import ToolBar from './parts/ToolBar.vue';
import VirtualList from './parts/VirtualList.vue';

const props = withDefaults(
  defineProps<{
    source: LogPanelSource<E>;
    rules?: Partial<LogPanelRules<E>>;
    hooks?: LogPanelHooks<E>;
    features?: Partial<LogPanelFeatures>;
    theme?: LogPanelTheme;
    /** 标题，缺省用内置文案 logpanel.title */
    title?: string;
    /** t(key, params) 本地化函数，缺省用内置中文文案 */
    t?: LogPanelTranslator;
    /** localStorage 键前缀 */
    storageKeyPrefix?: string;
    /** 初始几何（显式给出时优先于本地持久化） */
    initialGeometry?: Partial<LogPanelGeometry>;
    /** 尺寸预设 */
    windowSize?: LogPanelWindowSize | null;
    /** 透明度 0-100 */
    windowOpacity?: number | null;
  }>(),
  {
    theme: 'auto',
    storageKeyPrefix: 'logpanel_',
    windowSize: null,
    windowOpacity: null,
  },
);

/** 可见性（v-model:visible）；隐藏时保留 DOM 与内部状态 */
const visible = defineModel<boolean>('visible', { default: true });

const t = translator(props.t);
const rules = resolveRules<E>(props.rules);
const title = computed(() => props.title ?? t('logpanel.title'));
const features = computed<LogPanelFeatures>(() => ({ ...DEFAULT_FEATURES, ...props.features }));

const storage = createStorage(props.storageKeyPrefix);
const {
  paused,
  query,
  useRegex,
  levels,
  hiddenTags,
  expandedKeys,
  matcher,
  view,
  tags,
  levelCounts,
  bufferSize,
  bufferCapacity,
  keyOf,
  togglePause,
  toggleLevel,
  toggleTag,
  toggleAllTags,
  toggleExpanded,
  clearBuffer,
  plainLines,
} = useLogView<E>({ source: props.source, rules, hooks: props.hooks, storage });

const { toasts, push } = useToasts();
const listRef = ref<{ remeasure: () => void; toEnd: () => Promise<void> } | null>(null);

const { style: geometryStyle, interacting, startDrag, startResize, applyPreset } = useWindowGeometry({
  storage,
  initial: props.initialGeometry,
  onResizeEnd: () => listRef.value?.remeasure(),
});

/** 尺寸预设：初值也要生效（watch 非 immediate），此后随 prop 变化 */
if (props.windowSize) applyPreset(props.windowSize);
watch(
  () => props.windowSize,
  (size) => {
    if (size) applyPreset(size);
  },
);

/** 是否跟随新增日志（持久化） */
const follow = ref(storage.readBool(STORAGE_KEYS.follow, true));
watch(follow, (value) => storage.writeBool(STORAGE_KEYS.follow, value));

const rootStyle = computed(() => ({
  ...geometryStyle.value,
  opacity: props.windowOpacity === null ? undefined : props.windowOpacity / 100,
}));

// 主题：显式指定优先，auto 跟随系统偏好
const systemDark = ref(false);
let systemTheme: MediaQueryList | null = null;

function onSystemTheme(event: MediaQueryListEvent): void {
  systemDark.value = event.matches;
}

onMounted(() => {
  systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  systemDark.value = systemTheme.matches;
  systemTheme.addEventListener('change', onSystemTheme);
});
onBeforeUnmount(() => systemTheme?.removeEventListener('change', onSystemTheme));

const dark = computed(() => props.theme === 'dark' || (props.theme === 'auto' && systemDark.value));
const touch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;

const status = computed(
  () => `${view.value.length} / ${bufferSize.value}${paused.value ? t('logpanel.pausedSuffix') : ''}`,
);

/** 手动切换跟随：开启时立即贴底（其余贴底时机由列表内部的内容变化驱动） */
function onToggleFollow(): void {
  follow.value = !follow.value;
  if (follow.value) void listRef.value?.toEnd();
}

async function onCopy(): Promise<void> {
  const lines = plainLines();
  const copied = await copyText(lines.join('\n'));
  push(
    copied ? t('logpanel.copied', { n: lines.length }) : t('logpanel.copyFailed'),
    copied ? 'success' : 'error',
  );
}

function onExport(): void {
  const name = exportFileName(new Date());
  try {
    const blob = new Blob([plainLines().join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    push(t('logpanel.exported', { fname: name }), 'success');
  } catch (error) {
    push(t('logpanel.exportFailed', { err: String(error) }), 'error');
  }
}

function onClear(): void {
  clearBuffer();
  push(t('logpanel.logCleared'), 'success');
}

// 右键菜单：组件内固定定位（根节点无 transform/contain，不会被裁剪或改写坐标系）
const menu = ref<{ x: number; y: number } | null>(null);
const menuStyle = ref<Record<string, string>>({});
const menuEl = ref<HTMLElement | null>(null);

async function onMenu(event: MouseEvent): Promise<void> {
  const at = { x: event.clientX, y: event.clientY };
  menu.value = at;
  menuStyle.value = { left: `${at.x}px`, top: `${at.y}px` };
  await nextTick();
  const rect = menuEl.value?.getBoundingClientRect();
  if (!rect) return;
  menuStyle.value = {
    left: `${Math.min(Math.max(at.x, VIEWPORT_MARGIN), window.innerWidth - rect.width - VIEWPORT_MARGIN)}px`,
    top: `${Math.min(Math.max(at.y, VIEWPORT_MARGIN), window.innerHeight - rect.height - VIEWPORT_MARGIN)}px`,
  };
}

function closeMenu(): void {
  menu.value = null;
}

function onDocumentPointerDown(event: PointerEvent): void {
  if (!menuEl.value?.contains(event.target as Node)) closeMenu();
}

watch(menu, (open) => {
  if (open) document.addEventListener('pointerdown', onDocumentPointerDown, true);
  else document.removeEventListener('pointerdown', onDocumentPointerDown, true);
});

function closePanel(): void {
  closeMenu();
  visible.value = false;
}

onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown, true));
</script>

<template>
  <div
    v-show="visible"
    class="lp-root"
    :class="{ 'lp-interacting': interacting }"
    :data-theme="dark ? 'dark' : undefined"
    :data-touch="touch ? 'true' : undefined"
    :style="rootStyle"
    data-testid="logpanel"
  >
    <TitleBar
      :title="title"
      :status="status"
      :t="t"
      :paused="paused"
      :follow="follow"
      :draggable="features.drag"
      :show-pause="features.pause"
      :show-follow="features.autoScroll"
      :show-menu="features.contextMenu"
      @drag-start="startDrag"
      @pause="togglePause"
      @follow="onToggleFollow"
      @close="closePanel"
      @menu="onMenu"
    />

    <ToolBar
      v-model:query="query"
      v-model:regex="useRegex"
      :features="features"
      :t="t"
      :levels="levels"
      :regex-invalid="matcher.invalid"
      @toggle-level="toggleLevel"
      @clear="onClear"
      @copy="onCopy"
      @export="onExport"
    />

    <div class="lp-body">
      <SidePanel
        v-if="features.tagFilter || features.stats"
        :t="t"
        :tags="tags"
        :hidden-tags="hiddenTags"
        :tag-color="rules.tagColor"
        :level-counts="levelCounts"
        :used="bufferSize"
        :capacity="bufferCapacity"
        :show-tags="features.tagFilter"
        :show-stats="features.stats"
        @toggle-tag="toggleTag"
        @toggle-all-tags="toggleAllTags"
      />
      <VirtualList
        ref="listRef"
        v-model:follow="follow"
        :entries="view"
        :rules="rules"
        :hooks="hooks"
        :matcher="matcher"
        :key-of="keyOf"
        :expanded-keys="expandedKeys"
        @toggle="(entry) => toggleExpanded(keyOf(entry))"
      />
    </div>

    <div v-if="features.resize" class="lp-resize" data-testid="lp-resize" @pointerdown="startResize" />

    <ToastStack :toasts="toasts" />

    <div v-if="menu" ref="menuEl" class="lp-menu" :style="menuStyle">
      <button class="lp-menu-item" type="button" data-testid="lp-menu-close" @click="closePanel">
        {{ t('logpanel.closePanel') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/**
 * --lp-*-c 是解析层：默认值只写在这里，--lp-* 始终留给宿主。
 * （若组件把默认值直接写到 --lp-* 上，宿主在祖先元素设的值会被自身声明屏蔽。）
 */
.lp-root {
  --lp-bg-c: var(--lp-bg, #ffffff);
  --lp-fg-c: var(--lp-fg, #333333);
  --lp-border-c: var(--lp-border, #d0d0d0);
  --lp-titlebar-bg-c: var(--lp-titlebar-bg, #f5f5f5);
  --lp-muted-c: var(--lp-muted, #888888);
  --lp-accent-c: var(--lp-accent, #1976d2);
  --lp-btn-bg-c: var(--lp-btn-bg, #f5f5f5);
  --lp-btn-hover-c: var(--lp-btn-hover, #e0e0e0);
  --lp-close-hover-c: var(--lp-close-hover, #e53935);
  --lp-input-bg-c: var(--lp-input-bg, #ffffff);
  --lp-input-border-c: var(--lp-input-border, #cccccc);
  --lp-error-c: var(--lp-error, #d32f2f);
  --lp-hover-bg-c: var(--lp-hover-bg, #eeeeee);
  --lp-scrollbar-c: var(--lp-scrollbar, #bbbbbb);
  --lp-log-bg-c: var(--lp-log-bg, #fafafa);
  --lp-mark-bg-c: var(--lp-mark-bg, #ffe066);
  --lp-font-ui-c: var(--lp-font-ui, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif);
  --lp-font-mono-c: var(--lp-font-mono, 'Cascadia Code', 'Fira Code', Consolas, 'Courier New', monospace);
  --lp-level-debug-c: var(--lp-level-debug, #888888);
  --lp-level-info-c: var(--lp-level-info, #0088ff);
  --lp-level-warn-c: var(--lp-level-warn, #ff8800);
  --lp-level-error-c: var(--lp-level-error, #ff0000);

  position: fixed;
  z-index: 25000;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  overflow: hidden;
  font-family: var(--lp-font-ui-c);
  font-size: 12px;
  color: var(--lp-fg-c);
  background: var(--lp-bg-c);
  border: 1px solid var(--lp-border-c);
  border-radius: 8px;
  box-shadow: 0 12px 40px rgb(0 0 0 / 25%);
}

.lp-root[data-theme='dark'] {
  --lp-bg-c: var(--lp-bg, #252525);
  --lp-fg-c: var(--lp-fg, #e0e0e0);
  --lp-border-c: var(--lp-border, #3a3a3a);
  --lp-titlebar-bg-c: var(--lp-titlebar-bg, #2a2a2a);
  --lp-accent-c: var(--lp-accent, #42a5f5);
  --lp-btn-bg-c: var(--lp-btn-bg, #333333);
  --lp-btn-hover-c: var(--lp-btn-hover, #404040);
  --lp-close-hover-c: var(--lp-close-hover, #ef5350);
  --lp-input-bg-c: var(--lp-input-bg, #2a2a2a);
  --lp-input-border-c: var(--lp-input-border, #444444);
  --lp-error-c: var(--lp-error, #ef5350);
  --lp-hover-bg-c: var(--lp-hover-bg, #383838);
  --lp-scrollbar-c: var(--lp-scrollbar, #555555);
  --lp-log-bg-c: var(--lp-log-bg, #1a1a1a);
  --lp-mark-bg-c: var(--lp-mark-bg, #b8860b);
}

.lp-body {
  display: flex;
  flex: 1;
  min-height: 0;
  background: var(--lp-log-bg-c);
}

.lp-resize {
  position: absolute;
  right: 0;
  bottom: 0;
  width: 14px;
  height: 14px;
  cursor: nwse-resize;
  touch-action: none;
}
.lp-resize::after {
  content: '';
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-right: 2px solid var(--lp-muted-c);
  border-bottom: 2px solid var(--lp-muted-c);
}

.lp-interacting {
  user-select: none;
}
.lp-interacting .lp-body {
  pointer-events: none;
}

.lp-menu {
  position: fixed;
  z-index: 1;
  min-width: 120px;
  padding: 4px 0;
  font-size: 12px;
  background: var(--lp-bg-c);
  border: 1px solid var(--lp-border-c);
  border-radius: 4px;
  box-shadow: 0 4px 16px rgb(0 0 0 / 20%);
}

.lp-menu-item {
  display: block;
  width: 100%;
  padding: 6px 14px;
  font-family: inherit;
  font-size: 12px;
  color: var(--lp-error-c);
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  user-select: none;
}
.lp-menu-item:hover {
  background: var(--lp-hover-bg-c);
}

/* 触摸端：加大点击目标与行距（子组件内部元素用 :deep 穿透 scoped 作用域） */
.lp-root[data-touch='true'] :deep(.lp-titlebar) {
  height: 34px;
}
.lp-root[data-touch='true'] :deep(.lp-iconbtn) {
  width: 28px;
  height: 28px;
}
.lp-root[data-touch='true'] :deep(.lp-btn) {
  height: 30px;
  padding: 0 12px;
  font-size: 12px;
}
.lp-root[data-touch='true'] :deep(.lp-log-line) {
  padding-top: 3px;
  padding-bottom: 3px;
  font-size: 12px;
}
.lp-root[data-touch='true'] :deep(.lp-expand-icon) {
  width: 18px;
  font-size: 10px;
}
.lp-root[data-touch='true'] :deep(.lp-line-detail) {
  max-height: 400px;
  padding: 8px 10px 8px 24px;
  font-size: 12px;
}
.lp-root[data-touch='true'] :deep(.lp-side) {
  width: 170px;
  padding: 10px 12px;
  font-size: 12px;
}
.lp-root[data-touch='true'] :deep(.lp-tag-row) {
  padding: 5px 6px;
  font-size: 12px;
}
.lp-root[data-touch='true'] :deep(.lp-level-row) {
  font-size: 12px;
  line-height: 1.8;
}
.lp-root[data-touch='true'] :deep(.lp-resize) {
  width: 20px;
  height: 20px;
}
</style>