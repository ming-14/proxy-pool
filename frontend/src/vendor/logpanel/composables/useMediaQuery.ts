/**
 * 媒体查询布尔量：形态（视口宽度）与触控（主指针类型）两条轴共用。
 * query 视为常量（组件传固定串），不随参数变化重建。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import type { Ref } from 'vue';

export function useMediaQuery(query: string): Ref<boolean> {
  const list = window.matchMedia(query);
  const matches = ref(list.matches);
  const onChange = (event: MediaQueryListEvent): void => {
    matches.value = event.matches;
  };
  onMounted(() => list.addEventListener('change', onChange));
  onBeforeUnmount(() => list.removeEventListener('change', onChange));
  return matches;
}
