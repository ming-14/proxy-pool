/**
 * 悬浮窗几何：拖拽、缩放、视口夹紧、尺寸预设与持久化。
 *
 * 坐标是视口坐标系的 px，几何值即窗口宽高，夹取只做算术（不读布局），
 * 因此手势过程中不触发布局抖动。手势用 pointer 事件统一鼠标与触摸。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import {
  DEFAULT_WINDOW_H,
  DEFAULT_WINDOW_W,
  MIN_WINDOW_H,
  MIN_WINDOW_W,
  STORAGE_KEYS,
  VIEWPORT_MARGIN,
  WINDOW_EDGE_GAP,
  WINDOW_SIZES,
} from '../defaults';
import type { LogPanelGeometry, LogPanelWindowSize } from '../types';
import type { PanelStorage } from '../utils/storage';

export interface WindowGeometryOptions {
  storage: PanelStorage;
  /** 初始几何（显式给出时优先于本地持久化） */
  initial?: Partial<LogPanelGeometry>;
  /** 尺寸变化结束回调：宽度变化会改变行高，列表需要重测 */
  onResizeEnd?: () => void;
}

interface DragSession {
  pointerId: number;
  x: number;
  y: number;
  originX: number;
  originY: number;
}

interface ResizeSession extends DragSession {
  originW: number;
  originH: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

export function useWindowGeometry(options: WindowGeometryOptions) {
  const { storage, initial, onResizeEnd } = options;
  const geometry = reactive<LogPanelGeometry>(load());
  /** 拖拽/缩放进行中：面板据此禁用内容区交互，避免误触选中 */
  const interacting = ref(false);

  const style = computed(() => ({
    left: `${geometry.x}px`,
    top: `${geometry.y}px`,
    width: `${geometry.w}px`,
    height: `${geometry.h}px`,
  }));

  /** 视口内可用的最大宽高（保底为最小窗口尺寸） */
  function maxWidth(): number {
    return Math.max(MIN_WINDOW_W, window.innerWidth - VIEWPORT_MARGIN);
  }

  function maxHeight(): number {
    return Math.max(MIN_WINDOW_H, window.innerHeight - VIEWPORT_MARGIN);
  }

  function load(): LogPanelGeometry {
    const stored = storage.readJSON<Partial<LogPanelGeometry>>(STORAGE_KEYS.geometry, {});
    const value = { ...stored, ...initial };
    const w = clamp(value.w ?? DEFAULT_WINDOW_W, MIN_WINDOW_W, maxWidth());
    const h = clamp(value.h ?? DEFAULT_WINDOW_H, MIN_WINDOW_H, maxHeight());
    const x = clamp(value.x ?? window.innerWidth - w - WINDOW_EDGE_GAP, 0, window.innerWidth - w);
    const y = clamp(value.y ?? window.innerHeight - h - WINDOW_EDGE_GAP, 0, window.innerHeight - h);
    return { x, y, w, h };
  }

  function persist(): void {
    storage.writeJSON(STORAGE_KEYS.geometry, { ...geometry });
  }

  let drag: DragSession | null = null;
  let resize: ResizeSession | null = null;

  function onDragMove(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    geometry.x = clamp(drag.originX + (event.clientX - drag.x), 0, window.innerWidth - geometry.w);
    geometry.y = clamp(drag.originY + (event.clientY - drag.y), 0, window.innerHeight - geometry.h);
  }

  function endDrag(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag = null;
    interacting.value = false;
    window.removeEventListener('pointermove', onDragMove);
    window.removeEventListener('pointerup', endDrag);
    window.removeEventListener('pointercancel', endDrag);
    persist();
  }

  function startDrag(event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      originX: geometry.x,
      originY: geometry.y,
    };
    interacting.value = true;
    window.addEventListener('pointermove', onDragMove);
    window.addEventListener('pointerup', endDrag);
    window.addEventListener('pointercancel', endDrag);
  }

  function onResizeMove(event: PointerEvent): void {
    if (!resize || event.pointerId !== resize.pointerId) return;
    geometry.w = clamp(
      resize.originW + (event.clientX - resize.x),
      MIN_WINDOW_W,
      window.innerWidth - geometry.x - VIEWPORT_MARGIN,
    );
    geometry.h = clamp(
      resize.originH + (event.clientY - resize.y),
      MIN_WINDOW_H,
      window.innerHeight - geometry.y - VIEWPORT_MARGIN,
    );
  }

  function endResize(event: PointerEvent): void {
    if (!resize || event.pointerId !== resize.pointerId) return;
    resize = null;
    interacting.value = false;
    window.removeEventListener('pointermove', onResizeMove);
    window.removeEventListener('pointerup', endResize);
    window.removeEventListener('pointercancel', endResize);
    persist();
    onResizeEnd?.();
  }

  function startResize(event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    resize = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      originX: geometry.x,
      originY: geometry.y,
      originW: geometry.w,
      originH: geometry.h,
    };
    interacting.value = true;
    window.addEventListener('pointermove', onResizeMove);
    window.addEventListener('pointerup', endResize);
    window.addEventListener('pointercancel', endResize);
  }

  /** 视口变小后把窗口重新收进可视范围 */
  function onViewportResize(): void {
    const before = { ...geometry };
    geometry.w = clamp(geometry.w, MIN_WINDOW_W, maxWidth());
    geometry.h = clamp(geometry.h, MIN_WINDOW_H, maxHeight());
    geometry.x = clamp(geometry.x, 0, window.innerWidth - geometry.w);
    geometry.y = clamp(geometry.y, 0, window.innerHeight - geometry.h);
    const changed = geometry.w !== before.w || geometry.h !== before.h;
    if (changed || geometry.x !== before.x || geometry.y !== before.y) persist();
    if (changed) onResizeEnd?.();
  }

  function applyPreset(size: LogPanelWindowSize): void {
    const preset = WINDOW_SIZES[size];
    geometry.w = clamp(preset.w, MIN_WINDOW_W, maxWidth());
    geometry.h = clamp(preset.h, MIN_WINDOW_H, maxHeight());
    geometry.x = clamp(geometry.x, 0, window.innerWidth - geometry.w);
    geometry.y = clamp(geometry.y, 0, window.innerHeight - geometry.h);
    persist();
    onResizeEnd?.();
  }

  onMounted(() => window.addEventListener('resize', onViewportResize));
  onBeforeUnmount(() => {
    window.removeEventListener('resize', onViewportResize);
    window.removeEventListener('pointermove', onDragMove);
    window.removeEventListener('pointerup', endDrag);
    window.removeEventListener('pointercancel', endDrag);
    window.removeEventListener('pointermove', onResizeMove);
    window.removeEventListener('pointerup', endResize);
    window.removeEventListener('pointercancel', endResize);
  });

  return { geometry, style, interacting, startDrag, startResize, applyPreset };
}