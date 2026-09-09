/**
 * 主题 store：light/dark，localStorage 持久化（key 与旧版一致）。
 * 应用方式与旧版相同：html.dark class + data-theme 属性（图表取色依赖 CSS 变量）。
 */
import { defineStore } from 'pinia';
import { ref, watch } from 'vue';

const STORAGE_KEY = 'proxy-pool-theme';

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<'light' | 'dark'>(
    localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light',
  );

  function apply() {
    const html = document.documentElement;
    html.classList.toggle('dark', theme.value === 'dark');
    html.setAttribute('data-theme', theme.value);
    localStorage.setItem(STORAGE_KEY, theme.value);
  }

  function toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark';
  }

  watch(theme, apply, { immediate: true });

  return { theme, toggle };
});
