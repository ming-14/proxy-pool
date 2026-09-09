/**
 * Toast store：全局消息队列（success/error/warning/info）。
 * 时长默认 error 5s、其余 3s；关闭动画 220ms 后移除（与旧版一致）。
 */
import { defineStore } from 'pinia';
import { ref } from 'vue';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface Toast {
  id: number;
  type: ToastType;
  title: string;
  message?: string;
  leaving: boolean;
}

let nextId = 1;
const LEAVE_MS = 220;

export const useToastStore = defineStore('toast', () => {
  const toasts = ref<Toast[]>([]);

  function remove(id: number) {
    const t = toasts.value.find((x) => x.id === id);
    if (!t || t.leaving) return;
    t.leaving = true;
    setTimeout(() => {
      toasts.value = toasts.value.filter((x) => x.id !== id);
    }, LEAVE_MS);
  }

  function show(type: ToastType, title: string, message?: string, duration?: number) {
    const id = nextId++;
    toasts.value.push({ id, type, title, message, leaving: false });
    setTimeout(() => remove(id), duration ?? (type === 'error' ? 5000 : 3000));
  }

  const success = (title: string, msg?: string) => show('success', title, msg);
  const error = (title: string, msg?: string) => show('error', title, msg);
  const warning = (title: string, msg?: string) => show('warning', title, msg);
  const info = (title: string, msg?: string) => show('info', title, msg);

  return { toasts, show, remove, success, error, warning, info };
});
