<script setup lang="ts">
/** Toast 渲染宿主：右上角堆叠，图标按类型着色，点击关闭/自动消失。 */
import { useToastStore } from '@/stores/toast';

const store = useToastStore();

const ICONS: Record<string, string> = {
  success: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  error: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  warning: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
};
</script>

<template>
  <div data-testid="toast-container" class="toast-container fixed top-4 right-4 z-50 flex flex-col gap-2">
    <div v-for="t in store.toasts" :key="t.id" class="toast" :class="[`toast-${t.type}`, { leaving: t.leaving }]">
      <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" v-html="ICONS[t.type]" />
      <div class="toast-content">
        <div class="toast-title">{{ t.title }}</div>
        <div v-if="t.message" class="toast-message">{{ t.message }}</div>
      </div>
      <button class="toast-close" aria-label="关闭" @click="store.remove(t.id)">✕</button>
    </div>
  </div>
</template>
