<script setup lang="ts">
/**
 * 工具栏：等级筛选、搜索（普通/正则，非法标红）与清空/复制/导出。
 */
import { LEVELS } from '../defaults';
import type { LogPanelFeatures, LogPanelTranslator } from '../types';
import IconSvg from './IconSvg.vue';

defineProps<{
  features: LogPanelFeatures;
  t: LogPanelTranslator;
  levels: ReadonlySet<number>;
  regexInvalid: boolean;
}>();

const emit = defineEmits<{
  (e: 'toggle-level', level: number): void;
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

    <div class="lp-actions">
      <button
        v-if="features.clear"
        class="lp-btn"
        type="button"
        data-testid="lp-clear"
        :title="t('logpanel.clearLog')"
        @click="emit('clear')"
      >
        <IconSvg name="clear" />{{ t('logpanel.clear') }}
      </button>
      <button
        v-if="features.copy"
        class="lp-btn"
        type="button"
        data-testid="lp-copy"
        :title="t('logpanel.copyFilter')"
        @click="emit('copy')"
      >
        <IconSvg name="copy" />{{ t('logpanel.copy') }}
      </button>
      <button
        v-if="features.export"
        class="lp-btn"
        type="button"
        data-testid="lp-export"
        :title="t('logpanel.exportTxt')"
        @click="emit('export')"
      >
        <IconSvg name="export" />{{ t('logpanel.export') }}
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
</style>