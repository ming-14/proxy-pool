<script setup lang="ts">
/**
 * 标题栏：拖拽区、统计文本、暂停/跟随/关闭按钮与右键菜单入口。
 */
import type { LogPanelTranslator } from '../types';
import IconSvg from './IconSvg.vue';

const props = defineProps<{
  title: string;
  status: string;
  t: LogPanelTranslator;
  paused: boolean;
  follow: boolean;
  draggable: boolean;
  showPause: boolean;
  showFollow: boolean;
  showMenu: boolean;
}>();

const emit = defineEmits<{
  (e: 'drag-start', event: PointerEvent): void;
  (e: 'pause'): void;
  (e: 'follow'): void;
  (e: 'close'): void;
  (e: 'menu', event: MouseEvent): void;
}>();

function onPointerDown(event: PointerEvent): void {
  if (!props.draggable) return;
  if ((event.target as HTMLElement).closest('button')) return;
  emit('drag-start', event);
}

function onContextMenu(event: MouseEvent): void {
  if (!props.showMenu) return;
  event.preventDefault();
  event.stopPropagation();
  emit('menu', event);
}
</script>

<template>
  <div
    class="lp-titlebar"
    :class="{ 'lp-draggable': draggable }"
    @pointerdown="onPointerDown"
    @contextmenu="onContextMenu"
  >
    <div class="lp-title">
      <IconSvg name="title" />
      <span>{{ title }}</span>
    </div>
    <span class="lp-status" data-testid="lp-status">{{ status }}</span>
    <div class="lp-spacer" />
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
.lp-titlebar.lp-draggable {
  cursor: move;
  touch-action: none;
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
</style>