<script setup lang="ts">
/**
 * 标题栏：统计文本、暂停/跟随/关闭按钮、档位按钮，以及统一的手势状态机。
 *
 * 标题栏是唯一的手势面（悬浮态自由拖拽、抽屉态拖拽切档），三种手势共用一条状态机：
 * - 长按（仅触屏 + 允许右键菜单）：打开菜单，抬手抑制合成 click，否则菜单刚开就被点按切档；
 * - 拖拽：位移超过死区才算拖拽，悬浮态交给几何 composable 接管，抽屉态实时位移、抬手定档；
 * - 点按：抽屉态切换档位。
 *
 * touch-action: none 只打在手势面（.lp-gesture）上，不能上移到面板根节点——它的有效值是
 * 祖先链的交集，根节点设 none 会连带废掉日志列表的纵向滚动。
 */
import { computed, onBeforeUnmount } from 'vue';
import { DRAG_SLOP, LONG_PRESS_MS } from '../defaults';
import type { LogPanelDetent, LogPanelTranslator } from '../types';
import IconSvg from './IconSvg.vue';

const props = defineProps<{
  title: string;
  status: string;
  t: LogPanelTranslator;
  paused: boolean;
  follow: boolean;
  /** 悬浮态自由拖拽（抽屉态无自由定位） */
  draggable: boolean;
  sheet: boolean;
  detent: LogPanelDetent;
  /** 抽屉档位手势是否可用（features.resize） */
  detentEnabled: boolean;
  showPause: boolean;
  showFollow: boolean;
  showMenu: boolean;
}>();

const emit = defineEmits<{
  (e: 'drag-start', event: PointerEvent): void;
  (e: 'drag-begin', event: PointerEvent): void;
  (e: 'drag-move', event: PointerEvent): void;
  (e: 'drag-end', event: PointerEvent): void;
  (e: 'drag-cancel'): void;
  (e: 'toggle-detent'): void;
  (e: 'pause'): void;
  (e: 'follow'): void;
  (e: 'close'): void;
  (e: 'menu', at: { x: number; y: number }): void;
}>();

const canDetent = computed(() => props.sheet && props.detentEnabled);
const gestureEnabled = computed(() => props.draggable || canDetent.value);

interface Gesture {
  pointerId: number;
  /** 起手事件：悬浮拖拽在越过死区后才交给几何 composable，锚点仍是起手点 */
  down: PointerEvent;
  x: number;
  y: number;
  moved: boolean;
  /** 已交给几何 composable 接管后续 move/up */
  handed: boolean;
}

let gesture: Gesture | null = null;
let longPressTimer = 0;
let longPressFired = false;
let longPressAt: { x: number; y: number } | null = null;

function startLongPress(x: number, y: number): void {
  longPressAt = { x, y };
  longPressTimer = window.setTimeout(() => {
    longPressTimer = 0;
    longPressFired = true;
    if (longPressAt) emit('menu', longPressAt);
  }, LONG_PRESS_MS);
}

function cancelLongPress(): void {
  if (longPressTimer) {
    clearTimeout(longPressTimer);
    longPressTimer = 0;
  }
  longPressAt = null;
}

function detach(): void {
  gesture = null;
  window.removeEventListener('pointermove', onPointerMove);
  window.removeEventListener('pointerup', onPointerUp);
  window.removeEventListener('pointercancel', onPointerCancel);
}

function onPointerDown(event: PointerEvent): void {
  if (!gestureEnabled.value) return;
  if (event.button !== 0) return; // 非主键（右键开菜单）不参与手势
  if ((event.target as HTMLElement).closest('button')) return;
  gesture = {
    pointerId: event.pointerId,
    down: event,
    x: event.clientX,
    y: event.clientY,
    moved: false,
    handed: false,
  };
  // 触屏没有右键：长按标题栏打开菜单（鼠标走 contextmenu）
  if (canDetent.value && props.showMenu && event.pointerType !== 'mouse') startLongPress(event.clientX, event.clientY);
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerCancel);
}

function onPointerMove(event: PointerEvent): void {
  const current = gesture;
  if (!current || current.pointerId !== event.pointerId || current.handed) return;
  if (!current.moved) {
    if (Math.hypot(event.clientX - current.x, event.clientY - current.y) <= DRAG_SLOP) return;
    current.moved = true;
    cancelLongPress();
    if (!canDetent.value) {
      current.handed = true;
      emit('drag-start', current.down);
      return;
    }
    emit('drag-begin', event);
  }
  if (canDetent.value) emit('drag-move', event);
}

function onPointerUp(event: PointerEvent): void {
  const current = gesture;
  cancelLongPress();
  if (longPressFired) {
    longPressFired = false;
    event.preventDefault(); // 抑制长按抬手产生的合成 click，否则菜单刚开就被点按切档
    detach();
    return;
  }
  detach();
  if (!current || current.pointerId !== event.pointerId || current.handed) return;
  if (current.moved) {
    if (canDetent.value) emit('drag-end', event);
  } else if (canDetent.value) {
    emit('toggle-detent');
  }
}

function onPointerCancel(): void {
  const current = gesture;
  cancelLongPress();
  longPressFired = false;
  detach();
  if (current?.moved && !current.handed && canDetent.value) emit('drag-cancel');
}

function onContextMenu(event: MouseEvent): void {
  if (!props.showMenu) return;
  event.preventDefault();
  event.stopPropagation();
  emit('menu', { x: event.clientX, y: event.clientY });
}

onBeforeUnmount(() => {
  cancelLongPress();
  detach();
});
</script>

<template>
  <div
    class="lp-titlebar"
    :class="{ 'lp-gesture': gestureEnabled, 'lp-draggable': draggable }"
    @pointerdown="onPointerDown"
    @contextmenu="onContextMenu"
  >
    <div v-if="canDetent" class="lp-grabber" aria-hidden="true" />
    <div class="lp-title">
      <IconSvg name="title" />
      <span>{{ title }}</span>
    </div>
    <span class="lp-status" data-testid="lp-status">{{ status }}</span>
    <div class="lp-spacer" />
    <button
      v-if="canDetent"
      class="lp-iconbtn"
      type="button"
      data-testid="lp-detent"
      :title="detent === 'half' ? t('logpanel.expandSheet') : t('logpanel.collapseSheet')"
      :aria-expanded="detent === 'full'"
      @click="emit('toggle-detent')"
    >
      <IconSvg :name="detent === 'half' ? 'chevron-up' : 'chevron-down'" />
    </button>
    <button
      v-if="showPause"
      class="lp-iconbtn"
      type="button"
      data-testid="lp-pause"
      :title="paused ? t('logpanel.resumeReceiving') : t('logpanel.pauseReceiving')"
      :aria-pressed="paused"
      @click="emit('pause')"
    >
      <IconSvg :name="paused ? 'play' : 'pause'" />
    </button>
    <button
      v-if="showFollow"
      class="lp-iconbtn"
      type="button"
      data-testid="lp-autoscroll"
      :title="follow ? t('logpanel.disableAutoScroll') : t('logpanel.enableAutoScroll')"
      :aria-pressed="follow"
      @click="emit('follow')"
    >
      <IconSvg :name="follow ? 'scroll-end' : 'scroll-free'" />
    </button>
    <button
      class="lp-iconbtn lp-close"
      type="button"
      data-testid="lp-close"
      :title="t('logpanel.close')"
      @click="emit('close')"
    >
      <IconSvg name="close" />
    </button>
  </div>
</template>

<style scoped>
.lp-titlebar {
  position: relative;
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 8px 0 12px;
  background: var(--lp-titlebar-bg-c);
  border-bottom: 1px solid var(--lp-border-c);
  user-select: none;
}
.lp-titlebar.lp-gesture {
  touch-action: none;
}
.lp-titlebar.lp-draggable {
  cursor: move;
}

.lp-grabber {
  position: absolute;
  top: 3px;
  left: 50%;
  width: 36px;
  height: 3px;
  background: var(--lp-border-c);
  border-radius: 2px;
  transform: translateX(-50%);
  pointer-events: none;
}

.lp-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--lp-fg-c);
}
.lp-title :deep(svg) {
  color: var(--lp-accent-c);
}

.lp-status {
  font-size: 11px;
  color: var(--lp-muted-c);
  font-variant-numeric: tabular-nums;
}

.lp-spacer {
  flex: 1;
}

.lp-iconbtn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--lp-muted-c);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
}
.lp-iconbtn:hover {
  color: var(--lp-fg-c);
  background: var(--lp-btn-hover-c);
}
.lp-iconbtn.lp-close:hover {
  color: #fff;
  background: var(--lp-close-hover-c);
}

/* 触屏：加大目标尺寸与行距 */
[data-touch='true'] .lp-titlebar {
  height: 34px;
}
[data-touch='true'] .lp-iconbtn {
  width: 28px;
  height: 28px;
}

/* 抽屉形态（写在触控规则之后：同特异性下由顺序决定，抽屉优先） */
[data-form='sheet'] .lp-titlebar {
  height: 46px;
  padding: 8px 10px 0 14px;
}
[data-form='sheet'] .lp-iconbtn {
  width: 36px;
  height: 36px;
}
</style>
