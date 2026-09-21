/**
 * 面板内提示条：定时自动消失，卸载时清理未触发的定时器。
 */
import { onBeforeUnmount, ref } from 'vue';

export interface ToastItem {
  id: number;
  text: string;
  type: 'info' | 'success' | 'error';
}

export function useToasts(duration = 3000) {
  const toasts = ref<ToastItem[]>([]);
  const timers = new Set<ReturnType<typeof setTimeout>>();
  let nextId = 1;

  function push(text: string, type: ToastItem['type'] = 'info'): void {
    const id = nextId++;
    toasts.value.push({ id, text, type });
    const timer = setTimeout(() => {
      timers.delete(timer);
      toasts.value = toasts.value.filter((item) => item.id !== id);
    }, duration);
    timers.add(timer);
  }

  onBeforeUnmount(() => {
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
  });

  return { toasts, push };
}