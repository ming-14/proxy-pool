/**
 * 抽屉档位：半屏/全屏切换、拖拽中的高度跟随、抬手定档与持久化。
 *
 * 与 useWindowGeometry 分工：本 composable 只维护档位（独立持久化键），
 * 从不读写悬浮几何——窄屏尺寸因此不会污染桌面窗口的几何。
 *
 * 拖拽改的是**高度**而不是位移：抽屉底边钉在屏幕边缘，整体位移会让面板抬离底边、
 * 下方露出空隙（高度变化不影响换行，无需重测行高）。
 */
import { computed, ref } from 'vue';
import { STORAGE_KEYS } from '../defaults';
import type { LogPanelDetent } from '../types';
import { clampOffset, detentAfterDrag, sheetHeight } from '../utils/sheet';
import type { PanelStorage } from '../utils/storage';

export interface SheetDetentOptions {
  storage: PanelStorage;
  /** 可视视口高度（软键盘弹出时变矮，抽屉跟着收） */
  visualHeight: () => number;
}

export function useSheetDetent(options: SheetDetentOptions) {
  const { storage, visualHeight } = options;
  const detent = ref<LogPanelDetent>(storage.readJSON<string>(STORAGE_KEYS.detent, 'half') === 'full' ? 'full' : 'half');
  /** 拖拽中的高度增量（px，向上为负）：高度 = 档位高度 - 该值 */
  const offset = ref(0);
  const dragging = ref(false);
  const height = computed(() => sheetHeight(detent.value, visualHeight()) - offset.value);

  let start: { y: number; time: number } | null = null;

  function set(next: LogPanelDetent): void {
    detent.value = next;
    storage.writeJSON(STORAGE_KEYS.detent, next);
  }

  function toggle(): void {
    set(detent.value === 'half' ? 'full' : 'half');
  }

  function begin(clientY: number): void {
    start = { y: clientY, time: performance.now() };
    dragging.value = true;
    offset.value = 0;
  }

  function move(clientY: number): void {
    if (!start) return;
    offset.value = clampOffset(clientY - start.y, detent.value, visualHeight());
  }

  function reset(): void {
    start = null;
    dragging.value = false;
    offset.value = 0;
  }

  /** 抬手定档（快速滑动按方向、否则按位移取更近的档） */
  function commit(clientY: number): void {
    if (!start) return;
    const dy = clientY - start.y;
    const duration = performance.now() - start.time;
    reset();
    set(detentAfterDrag(detent.value, dy, duration, visualHeight()));
  }

  /** 手势被系统中断：回到原档 */
  function cancel(): void {
    reset();
  }

  return { detent, height, offset, dragging, toggle, begin, move, commit, cancel };
}
