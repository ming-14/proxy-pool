<script setup lang="ts">
/**
 * 侧栏：模块筛选（隐藏集语义，空集即全显）与等级统计、缓冲占比。
 */
import { computed } from 'vue';
import { LEVEL_NAMES } from '../defaults';
import type { LogPanelTranslator } from '../types';
import { resolveTagColor } from '../utils/tagColor';

const props = defineProps<{
  t: LogPanelTranslator;
  tags: string[];
  hiddenTags: ReadonlySet<string>;
  tagColor: Record<string, string>;
  levelCounts: number[];
  used: number;
  capacity: number;
  showTags: boolean;
  showStats: boolean;
}>();

const emit = defineEmits<{
  (e: 'toggle-tag', tag: string): void;
  (e: 'toggle-all-tags'): void;
}>();

const allHidden = computed(() => props.tags.length > 0 && props.tags.every((tag) => props.hiddenTags.has(tag)));

const percent = computed(() =>
  props.capacity > 0 ? Math.min(100, Math.round((props.used / props.capacity) * 100)) : 0,
);

const bufferText = computed(() =>
  props.capacity > 0
    ? props.t('logpanel.bufferLabel', { used: props.used, cap: props.capacity, pct: percent.value })
    : props.t('logpanel.bufferUsed', { used: props.used }),
);
</script>

<template>
  <aside class="lp-side">
    <section v-if="showTags" class="lp-section">
      <div class="lp-label">{{ t('logpanel.moduleFilter') }}</div>
      <button v-if="tags.length > 1" class="lp-tag-toggle" type="button" data-testid="lp-tag-toggle" @click="emit('toggle-all-tags')">
        {{ allHidden ? t('logpanel.selectAll') : t('logpanel.selectNone') }}
      </button>
      <div class="lp-tags">
        <button
          v-for="tag in tags"
          :key="tag"
          class="lp-tag-row"
          :class="{ active: !hiddenTags.has(tag) }"
          type="button"
          :data-tag="tag"
          @click="emit('toggle-tag', tag)"
        >
          <span class="lp-dot" :style="{ color: resolveTagColor(tag, tagColor) }">●</span>
          <span class="lp-tag-name">{{ tag }}</span>
        </button>
      </div>
    </section>

    <div class="lp-spacer" />

    <section v-if="showStats" class="lp-section">
      <div class="lp-label">{{ t('logpanel.stats') }}</div>
      <div v-for="(name, level) in LEVEL_NAMES" :key="name" class="lp-level-row">
        <span class="lp-dot" :data-level="level">●</span>
        <span class="lp-level-name">{{ name }}</span>
        <span class="lp-level-count">{{ levelCounts[level] ?? 0 }}</span>
      </div>
      <div class="lp-buffer" data-testid="lp-buffer">{{ bufferText }}</div>
      <div class="lp-bar"><div class="lp-bar-fill" :style="{ width: `${percent}%` }" /></div>
    </section>
  </aside>
</template>

<style scoped>
.lp-side {
  display: flex;
  flex-shrink: 0;
  flex-direction: column;
  gap: 6px;
  width: 150px;
  padding: 8px 10px;
  overflow-y: auto;
  font-family: var(--lp-font-ui-c);
  font-size: 11px;
  color: var(--lp-fg-c);
  border-right: 1px solid var(--lp-border-c);
  scrollbar-color: var(--lp-scrollbar-c) transparent;
  scrollbar-width: thin;
}
.lp-side::-webkit-scrollbar {
  width: 8px;
}
.lp-side::-webkit-scrollbar-thumb {
  background: var(--lp-scrollbar-c);
  border-radius: 4px;
}

.lp-section {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.lp-label {
  margin-bottom: 1px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.6px;
  color: var(--lp-muted-c);
  text-transform: uppercase;
}

.lp-tag-toggle {
  align-self: flex-start;
  margin-bottom: 4px;
  padding: 0;
  font-family: inherit;
  font-size: 10px;
  font-weight: 600;
  color: var(--lp-accent-c);
  background: transparent;
  border: none;
  cursor: pointer;
  user-select: none;
}
.lp-tag-toggle:hover {
  text-decoration: underline;
}

.lp-tags {
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 200px;
  overflow-y: auto;
  scrollbar-color: var(--lp-scrollbar-c) transparent;
  scrollbar-width: thin;
}

.lp-tag-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px;
  font-family: var(--lp-font-mono-c);
  font-size: 11px;
  color: var(--lp-muted-c);
  text-align: left;
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
  user-select: none;
  transition: background 0.1s, color 0.1s;
}
.lp-tag-row:hover {
  background: var(--lp-hover-bg-c);
}
.lp-tag-row.active {
  font-weight: 500;
  color: var(--lp-fg-c);
}

.lp-dot {
  flex-shrink: 0;
  width: 14px;
  font-size: 8px;
  text-align: left;
}
.lp-dot[data-level='0'] {
  color: var(--lp-level-debug-c);
}
.lp-dot[data-level='1'] {
  color: var(--lp-level-info-c);
}
.lp-dot[data-level='2'] {
  color: var(--lp-level-warn-c);
}
.lp-dot[data-level='3'] {
  color: var(--lp-level-error-c);
}

.lp-tag-name {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.lp-spacer {
  flex: 1;
  min-height: 8px;
}

.lp-level-row {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  line-height: 1.6;
}

.lp-level-name {
  min-width: 40px;
  color: var(--lp-fg-c);
}

.lp-level-count {
  margin-left: auto;
  font-family: var(--lp-font-mono-c);
  font-weight: 600;
  color: var(--lp-fg-c);
}

.lp-buffer {
  margin-top: 4px;
  font-family: var(--lp-font-mono-c);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  color: var(--lp-muted-c);
}

.lp-bar {
  height: 4px;
  margin-top: 2px;
  overflow: hidden;
  background: var(--lp-border-c);
  border-radius: 2px;
}

.lp-bar-fill {
  height: 100%;
  background: var(--lp-accent-c);
  border-radius: 2px;
  transition: width 0.2s;
}
</style>