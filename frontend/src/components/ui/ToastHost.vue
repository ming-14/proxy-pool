<script setup lang="ts">
/** Toast 渲染宿主：右上角堆叠，图标按类型着色，点击关闭/自动消失。 */
import { useToastStore, type ToastType } from '@/stores/toast';
import Icon from './Icon.vue';
import type { IconName } from './icons';

const store = useToastStore();

/** 类型 → 图标名（Record 保证四个类型齐全，漏一个即类型错误） */
const TOAST_ICONS: Record<ToastType, IconName> = {
  success: 'check-circle',
  error: 'x-circle',
  warning: 'alert-triangle',
  info: 'info',
};
</script>

<template>
  <div data-testid="toast-container" class="toast-container fixed top-4 right-4 z-50 flex flex-col gap-2">
    <div v-for="t in store.toasts" :key="t.id" class="toast" :class="[`toast-${t.type}`, { leaving: t.leaving }]">
      <Icon :name="TOAST_ICONS[t.type]" class="toast-icon" />
      <div class="toast-content">
        <div class="toast-title">{{ t.title }}</div>
        <div v-if="t.message" class="toast-message">{{ t.message }}</div>
      </div>
      <button class="toast-close inline-flex items-center" aria-label="关闭" @click="store.remove(t.id)">
        <Icon name="close" class="w-4 h-4" />
      </button>
    </div>
  </div>
</template>
