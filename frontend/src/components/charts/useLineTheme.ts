/**
 * 图表主题联动：CSS 变量单一来源，themeStore 变化时重建取色。
 * 取代旧版 MutationObserver + Chart.defaults 补丁。
 */
import { computed, type ComputedRef } from 'vue';
import { useThemeStore } from '@/stores/theme';
import { readLineTheme, type LineTheme } from '@/components/charts/chartOptions';

export function useLineTheme(): ComputedRef<LineTheme> {
  const themeStore = useThemeStore();
  return computed(() => {
    void themeStore.theme; // 建立响应式依赖：切换主题 → 重新读取 CSS 变量
    return readLineTheme();
  });
}
