<script setup lang="ts" generic="E extends object">
/**
 * LogPanel：通用日志视窗（悬浮窗 / 底部抽屉两种形态）。
 *
 * 数据与取值由宿主注入（source / rules / hooks），组件只负责视窗交互与渲染管线；
 * 样式经 scoped CSS + --lp-* 变量暴露——宿主在组件根或其任意祖先设置变量即可换肤，
 * 组件默认值写在 --lp-*-c 解析层，避免默认值屏蔽宿主传入的变量。
 *
 * 三条轴相互正交，不要混成一个判断：
 * - 形态（data-form）：看视口宽度，决定定位方式、几何来源与 chrome 布局；
 * - 触控（data-touch）：看主指针类型，决定目标尺寸与长按手势；
 * - 视口度量：悬浮几何用布局视口（不受缩放影响），抽屉尺寸用可视视口（软键盘只改它）。
 *
 * source / rules / hooks 视为常量（组件不监听其身份变化），主题/尺寸/透明度随 props 响应。
 *
 * 用法：
 *   <LogPanel v-model:visible="shown" :source="source" :rules="rules" title="控制台日志" />
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  DEFAULT_FEATURES,
  SHEET_MEDIA_QUERY,
  STORAGE_KEYS,
  TOUCH_MEDIA_QUERY,
  VIEWPORT_MARGIN,
  resolveRules,
  translator,
} from './defaults';
import type {
  LogPanelFeatures,
  LogPanelForm,
  LogPanelGeometry,
  LogPanelHooks,
  LogPanelRules,
  LogPanelSource,
  LogPanelTheme,
  LogPanelTranslator,
  LogPanelWindowSize,
} from './types';
import { useLogView } from './composables/useLogView';
import { useMediaQuery } from './composables/useMediaQuery';
import { useSheetDetent } from './composables/useSheetDetent';
import { useToasts } from './composables/useToasts';
import { useViewport } from './composables/useViewport';
import { useWindowGeometry } from './composables/useWindowGeometry';
import { copyText } from './utils/clipboard';
import { exportFileName } from './utils/format';
import { keyboardInset, resolveForm } from './utils/sheet';
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
    /** 形态：auto 按视口宽度判定，也可显式锁定为悬浮窗或抽屉 */
    form?: LogPanelForm;
    /** 标题，缺省用内置文案 logpanel.title */
    title?: string;
    /** t(key, params) 本地化函数，缺省用内置中文文案 */
    t?: LogPanelTranslator;
    /** localStorage 键前缀 */
    storageKeyPrefix?: string;
    /** 初始几何（显式给出时优先于本地持久化，只对悬浮形态生效） */
    initialGeometry?: Partial<LogPanelGeometry>;
    /** 尺寸预设 */
    windowSize?: LogPanelWindowSize | null;
    /** 透明度 0-100 */
    windowOpacity?: number | null;
  }>(),
  {
    theme: 'auto',
    form: 'auto',
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

// ---- 形态 ----
const narrow = useMediaQuery(SHEET_MEDIA_QUERY);
const coarse = useMediaQuery(TOUCH_MEDIA_QUERY);
const form = computed(() => resolveForm(props.form, narrow.value));
const sheet = computed(() => form.value === 'sheet');

// ---- 几何：悬浮用持久化 px 几何，抽屉用视口算出的高度 ----
const viewport = useViewport();
const { geometry, style: geometryStyle, interacting, startDrag, startResize, applyPreset } =
  useWindowGeometry({ storage, initial: props.initialGeometry });
const {
  detent,
  height: detentHeight,
  offset: detentOffset,
  dragging: detentDragging,
  toggle: toggleDetent,
  begin: beginDetent,
  move: moveDetent,
  commit: commitDetent,
  cancel: cancelDetent,
} = useSheetDetent({ storage, visualHeight: () => viewport.visualHeight });

/** 手势进行中（悬浮拖拽/缩放或抽屉拖拽）：禁用内容区交互，避免误触选中 */
const interactingAny = computed(() => interacting.value || detentDragging.value);

const rootStyle = computed(() => ({
  ...(sheet.value
    ? {
        bottom: `${keyboardInset(viewport.layoutHeight, viewport.visualHeight, viewport.visualTop)}px`,
        height: `${detentHeight.value}px`,
      }
    : geometryStyle.value),
  opacity: props.windowOpacity === null ? undefined : props.windowOpacity / 100,
  // 拖拽中的实时位移走 transform（不触发布局）：抬手定档后由 height 过渡接管
  ...(detentDragging.value ? { transform: `translateY(${detentOffset.value}px)` } : {}),
}));

/**
 * 行高重测的唯一判据：渲染宽度变化。正文换行使行高依赖行宽，而高度变化（档位切换、
 * 软键盘、地址栏伸缩）与换行无关，不该触发全量重测。
 * 手势过程中跳过（拖拽缩放逐帧改宽度），抬手时补一次。
 */
const renderedWidth = computed(() => (sheet.value ? viewport.layoutWidth : geometry.w));
let measuredWidth = 0;
watch(
  [renderedWidth, interactingAny],
  ([width, busy]) => {
    if (busy || width === measuredWidth) return;
    measuredWidth = width;
    listRef.value?.remeasure();
  },
  { flush: 'post' },
);

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

/** 抽屉里的模块筛选条默认收起（持久化）；悬浮形态侧栏常显，与此无关 */
const sideOpen = ref(storage.readBool(STORAGE_KEYS.sideOpen, false));
watch(sideOpen, (value) => storage.writeBool(STORAGE_KEYS.sideOpen, value));

/** 抽屉里的侧栏只剩模块筛选横条：窄屏放不下等级统计块（筛选反馈由工具栏等级 chip 承担） */
const showFilterStrip = computed(() => sheet.value && features.value.tagFilter);
const showSidePanel = computed(() =>
  sheet.value
    ? showFilterStrip.value && sideOpen.value
    : features.value.tagFilter || features.value.stats,
);
const showStats = computed(() => features.value.stats && !sheet.value);

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

// 右键/长按菜单：组件内固定定位（根节点平时无 transform/contain，不会被裁剪或改写坐标系）
const menu = ref<{ x: number; y: number } | null>(null);
const menuStyle = ref<Record<string, string>>({});
const menuEl = ref<HTMLElement | null>(null);

async function onMenu(at: { x: number; y: number }): Promise<void> {
  menu.value = at;
  menuStyle.value = { left: `${at.x}px`, top: `${at.y}px` };
  await nextTick();
  const rect = menuEl.value?.getBoundingClientRect();
  if (!rect) return;
  // 下界取可视视口底边：软键盘收起动画期间布局视口仍允许菜单落到键盘区
  const bottom = viewport.visualTop + viewport.visualHeight;
  menuStyle.value = {
    left: `${Math.min(Math.max(at.x, VIEWPORT_MARGIN), window.innerWidth - rect.width - VIEWPORT_MARGIN)}px`,
    top: `${Math.min(Math.max(at.y, VIEWPORT_MARGIN), bottom - rect.height - VIEWPORT_MARGIN)}px`,
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

/** 抽屉拖拽起手：拖拽期根节点带 transform，fixed 菜单的包含块会被改写，先收掉 */
function onDetentBegin(event: PointerEvent): void {
  closeMenu();
  beginDetent(event.clientY);
}

onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown, true));
</script>

<template>
  <div
    v-show="visible"
    class="lp-root"
    :class="{ 'lp-interacting': interactingAny, 'lp-detent-dragging': detentDragging }"
    :data-form="form"
    :data-detent="sheet ? detent : undefined"
    :data-theme="dark ? 'dark' : undefined"
    :data-touch="coarse ? 'true' : undefined"
    :style="rootStyle"
    data-testid="logpanel"
  >
    <TitleBar
      :title="title"
      :status="status"
      :t="t"
      :paused="paused"
      :follow="follow"
      :draggable="features.drag && !sheet"
      :sheet="sheet"
      :detent="detent"
      :detent-enabled="features.resize"
      :show-pause="features.pause"
      :show-follow="features.autoScroll"
      :show-menu="features.contextMenu"
      @drag-start="startDrag"
      @drag-begin="onDetentBegin"
      @drag-move="(event) => moveDetent(event.clientY)"
      @drag-end="(event) => commitDetent(event.clientY)"
      @drag-cancel="cancelDetent"
      @toggle-detent="toggleDetent"
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
      :show-filter-toggle="showFilterStrip"
      :side-open="sideOpen"
      @toggle-level="toggleLevel"
      @toggle-side="sideOpen = !sideOpen"
      @clear="onClear"
      @copy="onCopy"
      @export="onExport"
    />

    <div class="lp-body">
      <SidePanel
        v-if="showSidePanel"
        :t="t"
        :tags="tags"
        :hidden-tags="hiddenTags"
        :tag-color="rules.tagColor"
        :level-counts="levelCounts"
        :used="bufferSize"
        :capacity="bufferCapacity"
        :show-tags="features.tagFilter"
        :show-stats="showStats"
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

    <div
      v-if="features.resize && !sheet"
      class="lp-resize"
      data-testid="lp-resize"
      @pointerdown="startResize"
    />

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

/**
 * 组件内统一 border-box：box-sizing 不继承，只在根节点设会让各 part 声明的尺寸被 padding/border 撑大
 * （抽屉标题栏声明 46px、实际渲染 55px）。:deep 才能覆盖各 part 自己模板里的元素。
 */
.lp-root :deep(*) {
  box-sizing: border-box;
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

/**
 * 抽屉形态：满宽贴底，高度与底部内缩由内联样式按视口算出（见 rootStyle）。
 * 安全区垫在根节点上——背景铺满到屏幕边缘，内容让开。
 */
.lp-root[data-form='sheet'] {
  --lp-safe-bottom-c: var(--lp-safe-bottom, env(safe-area-inset-bottom, 0px));
  --lp-safe-left-c: var(--lp-safe-left, env(safe-area-inset-left, 0px));
  --lp-safe-right-c: var(--lp-safe-right, env(safe-area-inset-right, 0px));

  inset: auto 0 0 0;
  padding: 0 var(--lp-safe-right-c) var(--lp-safe-bottom-c) var(--lp-safe-left-c);
  border-bottom: none;
  border-radius: 12px 12px 0 0;
  /* height 与 transform 必须同曲线同时长：松手时前者从当前档位高度走向目标档位、后者从手指位移回到 0，
     两者叠加后顶边从手指位置单调滑到目标位置。只过渡 height 的话 transform 会瞬间归零，
     表现为先瞬移回原档位、再播放动画滑过去。 */
  transition: height 0.18s ease, transform 0.18s ease;
}
.lp-root[data-form='sheet'].lp-detent-dragging {
  transition: none;
}
/* 抽屉里侧栏是列表上方的横条，不是左栏 */
.lp-root[data-form='sheet'] .lp-body {
  flex-direction: column;
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

/* 触屏：加大缩放手柄的抓取面积（其余触控度量在各 part 自己的样式里，就近维护） */
.lp-root[data-touch='true'] .lp-resize {
  width: 20px;
  height: 20px;
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
</style>
