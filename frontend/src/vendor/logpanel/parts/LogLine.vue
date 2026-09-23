<script setup lang="ts" generic="E extends object">
/**
 * 单行日志：时间、等级、模块、正文高亮与展开详情。
 * 渲染结果只由 props 决定（展开态由父级按 key 传入），虚拟列表回收重建行也不会丢状态。
 */
import { computed } from 'vue';
import type { LogPanelHooks, LogPanelRules } from '../types';
import type { Matcher } from '../utils/filter';
import { entryText } from '../utils/format';
import { resolveTagColor } from '../utils/tagColor';

const props = defineProps<{
  entry: E;
  rules: LogPanelRules<E>;
  hooks?: LogPanelHooks<E>;
  matcher: Matcher;
  expanded: boolean;
}>();

const emit = defineEmits<{ (e: 'toggle'): void }>();

const level = computed(() => props.rules.level(props.entry));
const levelName = computed(() => props.rules.levelName(props.entry));
const tag = computed(() => props.rules.tag(props.entry));
const tagColor = computed(() => resolveTagColor(tag.value, props.rules.tagColor));
const tsStr = computed(() => props.rules.tsStr(props.entry));
const expandable = computed(() => props.rules.isExpandable(props.entry));
const text = computed(() => entryText(props.entry, props.rules, props.hooks));
const segments = computed(() => props.matcher.segments(text.value));
const detail = computed(() => props.rules.stack(props.entry) || text.value);

function onActivate(event: MouseEvent): void {
  if (!expandable.value) return;
  // 详情区内点击（选择/复制堆栈）不切换展开态
  if ((event.target as HTMLElement).closest('.lp-line-detail')) return;
  emit('toggle');
}
</script>

<template>
  <div class="lp-log-line" :class="{ 'has-stack': expandable, expanded }" :data-level="level" @click="onActivate">
    <span class="lp-expand-icon">{{ expandable ? (expanded ? '▼' : '▶') : '' }}</span>
    <span class="lp-ts">{{ tsStr }}</span>
    <span class="lp-lvl">{{ levelName }}</span>
    <span class="lp-tag" :style="{ color: tagColor }">[{{ tag }}]</span>
    <span class="lp-text">
      <template v-for="(segment, index) in segments" :key="index"
        ><mark v-if="segment.hit">{{ segment.text }}</mark
        ><template v-else>{{ segment.text }}</template></template
      >
    </span>
    <div v-if="expandable && expanded" class="lp-line-detail">{{ detail }}</div>
  </div>
</template>

<style scoped>
.lp-log-line {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 6px;
  padding: 1px 8px 1px 4px;
  border-left: 3px solid transparent;
  word-break: break-word;
  white-space: pre-wrap;
}

.lp-log-line[data-level='0'] {
  border-left-color: var(--lp-level-debug-c);
}
.lp-log-line[data-level='1'] {
  border-left-color: var(--lp-level-info-c);
}
.lp-log-line[data-level='2'] {
  border-left-color: var(--lp-level-warn-c);
  background: color-mix(in srgb, var(--lp-level-warn-c) 6%, transparent);
}
.lp-log-line[data-level='3'] {
  border-left-color: var(--lp-level-error-c);
  background: color-mix(in srgb, var(--lp-level-error-c) 6%, transparent);
}
.lp-log-line:hover {
  background: var(--lp-hover-bg-c);
}
.lp-log-line.has-stack {
  cursor: pointer;
}

.lp-ts {
  flex-shrink: 0;
  color: var(--lp-muted-c);
  opacity: 0.7;
}

.lp-lvl {
  flex-shrink: 0;
  min-width: 38px;
  font-weight: 700;
}
.lp-log-line[data-level='0'] .lp-lvl {
  color: var(--lp-level-debug-c);
}
.lp-log-line[data-level='1'] .lp-lvl {
  color: var(--lp-level-info-c);
}
.lp-log-line[data-level='2'] .lp-lvl {
  color: var(--lp-level-warn-c);
}
.lp-log-line[data-level='3'] .lp-lvl {
  color: var(--lp-level-error-c);
}

.lp-tag {
  flex-shrink: 0;
  font-weight: 700;
}

.lp-text {
  flex: 1;
  min-width: 0;
  color: var(--lp-fg-c);
}

.lp-text mark {
  color: inherit;
  background: var(--lp-mark-bg-c, #ffe066);
  border-radius: 2px;
  padding: 0 1px;
}

.lp-expand-icon {
  flex-shrink: 0;
  width: 14px;
  text-align: center;
  font-size: 8px;
  line-height: inherit;
  color: var(--lp-muted-c);
  user-select: none;
}

.lp-line-detail {
  width: 100%;
  max-height: 300px;
  margin: 2px 0 -2px;
  padding: 6px 8px 6px 20px;
  overflow: auto;
  font-size: 11px;
  line-height: 1.4;
  white-space: pre;
  color: var(--lp-fg-c);
  background: var(--lp-titlebar-bg-c);
  border-radius: 3px;
}

/* 触屏：加大行距与展开图标、详情区高度（字号由列表统一给，两形态一致） */
[data-touch='true'] .lp-log-line {
  padding-top: 3px;
  padding-bottom: 3px;
}
[data-touch='true'] .lp-expand-icon {
  width: 18px;
  font-size: 10px;
}
[data-touch='true'] .lp-line-detail {
  max-height: 400px;
  padding: 8px 10px 8px 24px;
  font-size: 12px;
}
</style>