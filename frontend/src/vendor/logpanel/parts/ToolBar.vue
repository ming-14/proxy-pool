<script setup lang="ts">
/**
 * 工具栏：等级筛选、搜索（普通/正则，非法标红）与清空/复制/导出，抽屉态另加模块筛选条开关。
 */
import { LEVELS } from '../defaults';
import type { LogPanelFeatures, LogPanelTranslator } from '../types';
import IconSvg from './IconSvg.vue';

defineProps<{
  features: LogPanelFeatures;
  t: LogPanelTranslator;
  levels: ReadonlySet<number>;
  regexInvalid: boolean;
  /** 抽屉态显示模块筛选条开关（窄屏下筛选条默认收起） */
  showFilterToggle: boolean;
  sideOpen: boolean;
}>();

const emit = defineEmits<{
  (e: 'toggle-level', level: number): void;
  (e: 'toggle-side'): void;
  (e: 'clear'): void;
  (e: 'copy'): void;
  (e: 'export'): void;
}>();

const query = defineModel<string>('query', { required: true });
const regex = defineModel<boolean>('regex', { required: true });
</script>

<template>
  <div class="lp-toolbar">
    <template v-if="features.levelFilter">
      <div class="lp-level-group">
        <button
          v-for="def in LEVELS"
          :key="def.level"
          class="lp-chip"
          :class="{ active: levels.has(def.level) }"
          type="button"
          :data-level="def.level"
          :data-testid="`lp-level-${def.level}`"
          @click="emit('toggle-level', def.level)"
        >
          {{ def.name }}
        </button>
      </div>
      <div class="lp-divider" />
    </template>

    <div v-if="features.search" class="lp-search" :class="{ 'lp-search-invalid': regexInvalid }">
      <input
        v-model="query"
        type="text"
        data-testid="lp-search"
        autocomplete="off"
        spellcheck="false"
        :placeholder="t('logpanel.searchPlaceholder')"
      />
      <button
        v-if="features.regex"
        class="lp-regex-btn"
        :class="{ active: regex }"
        type="button"
        data-testid="lp-regex"
        :title="t('logpanel.toggleRegex')"
        @click="regex = !regex"
      >
        .*
      </button>
    </div>

    <button
      v-if="showFilterToggle"
      class="lp-icon-btn"
      type="button"
      data-testid="lp-side-toggle"
      :title="t('logpanel.moduleFilter')"
      :aria-expanded="sideOpen"
      @click="emit('toggle-side')"
    >
      <IconSvg name="filter" />
    </button>

    <div class="lp-actions">
      <button
        v-if="features.clear"
        class="lp-btn"
        type="button"
        data-testid="lp-clear"
        :title="t('logpanel.clearLog')"
        @click="emit('clear')"
      >
        <IconSvg name="clear" /><span class="lp-btn-label">{{ t('logpanel.clear') }}</span>
      </button>
      <button
        v-if="features.copy"
        class="lp-btn"
        type="button"
        data-testid="lp-copy"
        :title="t('logpanel.copyFilter')"
        @click="emit('copy')"
      >
        <IconSvg name="copy" /><span class="lp-btn-label">{{ t('logpanel.copy') }}</span>
      </button>
      <button
        v-if="features.export"
        class="lp-btn"
        type="button"
        data-testid="lp-export"
        :title="t('logpanel.exportTxt')"
        @click="emit('export')"
      >
        <IconSvg name="export" /><span class="lp-btn-label">{{ t('logpanel.export') }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.lp-toolbar {
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: var(--lp-titlebar-bg-c);
  border-bottom: 1px solid var(--lp-border-c);
}

.lp-level-group {
  display: flex;
  gap: 4px;
}

.lp-chip {
  padding: 2px 8px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.3px;
  color: var(--lp-muted-c);
  background: var(--lp-input-bg-c);
  border: 1px solid var(--lp-input-border-c);
  border-radius: 999px;
  cursor: pointer;
  opacity: 0.45;
  user-select: none;
  transition: opacity 0.1s, border-color 0.1s;
}
.lp-chip.active {
  opacity: 1;
}
.lp-chip[data-level='0'].active {
  color: var(--lp-level-debug-c);
  border-color: var(--lp-level-debug-c);
}
.lp-chip[data-level='1'].active {
  color: var(--lp-level-info-c);
  border-color: var(--lp-level-info-c);
}
.lp-chip[data-level='2'].active {
  color: var(--lp-level-warn-c);
  border-color: var(--lp-level-warn-c);
}
.lp-chip[data-level='3'].active {
  color: var(--lp-level-error-c);
  border-color: var(--lp-level-error-c);
}

.lp-divider {
  width: 1px;
  height: 18px;
  margin: 0 2px;
  background: var(--lp-border-c);
}

.lp-search {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 4px;
  min-width: 100px;
  height: 24px;
  padding: 0 6px;
  background: var(--lp-input-bg-c);
  border: 1px solid var(--lp-input-border-c);
  border-radius: 4px;
}
.lp-search-invalid {
  border-color: var(--lp-error-c);
}
.lp-search input {
  flex: 1;
  min-width: 0;
  font-family: var(--lp-font-mono-c);
  font-size: 12px;
  color: var(--lp-fg-c);
  background: transparent;
  border: none;
  outline: none;
}

.lp-regex-btn {
  padding: 2px 4px;
  font-family: var(--lp-font-mono-c);
  font-size: 10px;
  font-weight: 700;
  color: var(--lp-muted-c);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.lp-regex-btn.active {
  color: #fff;
  background: var(--lp-accent-c);
}

.lp-icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  color: var(--lp-muted-c);
  background: var(--lp-btn-bg-c);
  border: 1px solid var(--lp-input-border-c);
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
}
.lp-icon-btn[aria-expanded='true'] {
  color: #fff;
  background: var(--lp-accent-c);
  border-color: var(--lp-accent-c);
}

.lp-actions {
  display: flex;
  gap: 4px;
}

.lp-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 8px;
  font-family: inherit;
  font-size: 11px;
  color: var(--lp-fg-c);
  background: var(--lp-btn-bg-c);
  border: 1px solid var(--lp-input-border-c);
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s;
}
.lp-btn:hover {
  background: var(--lp-btn-hover-c);
}

/* 触屏：加大目标尺寸 */
[data-touch='true'] .lp-btn {
  height: 30px;
  padding: 0 12px;
  font-size: 12px;
}

/* 抽屉形态（写在触控规则之后：同特异性下由顺序决定，抽屉优先）：
   搜索独占首行，第二行放等级 chip / 筛选开关 / 操作；尺寸按 360px 窄屏收紧（并排总宽 ≤ 344px），
   否则会折成三行、把抽屉的三成高度吃在 chrome 上。输入框字号 16px——iOS 聚焦更小字号会整页缩放。 */
[data-form='sheet'] .lp-toolbar {
  gap: 6px;
  padding: 8px;
}
[data-form='sheet'] .lp-search {
  flex: 1 1 100%;
  order: -1;
  height: 36px;
}
[data-form='sheet'] .lp-search input {
  font-size: 16px;
}
[data-form='sheet'] .lp-level-group {
  gap: 4px;
}
[data-form='sheet'] .lp-chip {
  padding: 3px 6px;
  font-size: 10px;
}
[data-form='sheet'] .lp-divider {
  display: none;
}
[data-form='sheet'] .lp-icon-btn {
  width: 32px;
  height: 32px;
}
[data-form='sheet'] .lp-actions {
  gap: 6px;
  margin-left: auto;
}
[data-form='sheet'] .lp-btn {
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
}
[data-form='sheet'] .lp-btn-label {
  display: none;
}
</style>
