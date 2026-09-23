<script setup lang="ts">
/**
 * 面板内提示条：进场/离场淡入淡出由 TransitionGroup 驱动。
 */
import type { ToastItem } from '../composables/useToasts';

defineProps<{ toasts: ToastItem[] }>();
</script>

<template>
  <TransitionGroup tag="div" class="lp-toasts" name="lp-toast">
    <div v-for="toast in toasts" :key="toast.id" class="lp-toast" :class="toast.type">{{ toast.text }}</div>
  </TransitionGroup>
</template>

<style scoped>
.lp-toasts {
  position: absolute;
  bottom: 8px;
  left: 50%;
  z-index: 10;
  display: flex;
  flex-direction: column;
  gap: 4px;
  transform: translateX(-50%);
  pointer-events: none;
}

/* 抽屉：absolute 的偏移基准是 padding box，根节点的安全区内边距顶不起它，得自己让开 */
[data-form='sheet'] .lp-toasts {
  bottom: calc(8px + var(--lp-safe-bottom-c, 0px));
}

.lp-toast {
  padding: 6px 14px;
  font-size: 12px;
  font-family: var(--lp-font-ui-c);
  color: var(--lp-bg-c);
  background: var(--lp-fg-c);
  border-radius: 4px;
  box-shadow: 0 2px 8px rgb(0 0 0 / 20%);
  transition: opacity 0.2s;
}
.lp-toast.success {
  color: #fff;
  background: #4caf50;
}
.lp-toast.error {
  color: #fff;
  background: var(--lp-error-c);
}

.lp-toast-enter-from,
.lp-toast-leave-to {
  opacity: 0;
}
</style>