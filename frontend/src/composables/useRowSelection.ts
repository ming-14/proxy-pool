/**
 * 资源管理器式行选择 composable（框选 / Ctrl·Shift 多选 / 全选 / 边缘自动滚动）。
 *
 * 交互语义与旧前端完全一致；差异仅在实现：
 * - 选中集为响应式 Set，行高亮由模板 class 绑定驱动（不再手工 toggle class）；
 * - 监听器随组件卸载自动清理；框选矩形为一次性 DOM（高频 style 更新不进响应式）。
 *
 * 约定：渲染行 <tr> 带 data-idx（当前视图索引）与 data-proxy。
 */
import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue';

const DRAG_THRESHOLD = 4;   // 进入拖拽的位移阈值（px）
const SCROLL_EDGE = 40;     // 触发自动滚动的边缘距离（px）
const SCROLL_MAX = 30;      // 自动滚动每帧最大步长（px）

export interface RowSelectionOptions<T> {
  container: Ref<HTMLElement | null>;
  scrollEl: () => HTMLElement | null;
  getRows: () => T[];
  proxyOf: (row: T) => string;
  /** 坐标 → 数据索引（可跨未渲染区域估计）；-1 表示不在列表范围内 */
  indexAtY: (clientY: number) => number;
  /** 是否禁止从该目标开始交互（按钮/输入框等） */
  isExcludedTarget?: (e: MouseEvent) => boolean;
  /** 首次进入拖拽回调（页面用于关闭 hover 浮层） */
  onDragStart?: () => void;
}

export interface RowSelectionApi {
  count: () => number;
  has: (p: string) => boolean;
  getSelectedArray: () => string[];
  selectOne: (proxy: string | null) => void;
  clear: () => void;
  selectAll: () => void;
  isDragging: () => boolean;
}

interface DragState {
  startX: number;
  startY: number;
  lastX: number;
  lastY: number;
  startScrollTop: number;
  startIndex: number;
  ctrl: boolean;
  shift: boolean;
  baseSet: Set<string>;
  moved: boolean;
}

export function useRowSelection<T>(opts: RowSelectionOptions<T>): RowSelectionApi {
  const selected = ref(new Set<string>()) as Ref<Set<string>>;
  let anchorIndex = -1;
  let drag: DragState | null = null;
  let marquee: HTMLElement | null = null;
  let scrollRaf: number | null = null;
  // 自动滚动步长放在循环外：rAF 循环每帧读取，鼠标换边后方向即时跟随
  // （若把 step 捕获进 tick 闭包，方向要等经过中间死区停一次才切得过来）
  let scrollStep = 0;

  // ---------------- 框选矩形 ----------------
  function showMarquee() {
    if (marquee) return;
    marquee = document.createElement('div');
    marquee.className = 'marquee-select';
    document.body.appendChild(marquee);
  }

  function updateMarquee() {
    const scrollEl = opts.scrollEl();
    if (!drag || !marquee || !scrollEl) return;
    // 起始点固定在文档坐标，随内容滚动平移（与 explorer.exe 一致）
    const adjStartY = drag.startY - (scrollEl.scrollTop - drag.startScrollTop);
    marquee.style.left = `${Math.min(drag.startX, drag.lastX)}px`;
    marquee.style.top = `${Math.min(adjStartY, drag.lastY)}px`;
    marquee.style.width = `${Math.abs(drag.lastX - drag.startX)}px`;
    marquee.style.height = `${Math.abs(drag.lastY - adjStartY)}px`;
  }

  function hideMarquee() {
    marquee?.remove();
    marquee = null;
  }

  // ---------------- 选中计算 ----------------
  /** 将 [lo,hi] 索引区间应用到选中集；ctrl 时切换框内（保留框外 baseSet） */
  function applyRange(lo: number, hi: number, ctrl: boolean, baseSet: Set<string> | null) {
    const rows = opts.getRows();
    lo = Math.max(0, lo);
    hi = Math.min(rows.length - 1, hi);
    const inRange = new Set<string>();
    for (let i = lo; i <= hi; i++) {
      const p = rows[i] && opts.proxyOf(rows[i]);
      if (p) inRange.add(p);
    }
    const next = new Set<string>();
    if (ctrl && baseSet) {
      for (const p of baseSet) if (!inRange.has(p)) next.add(p);
      for (const p of inRange) if (!baseSet.has(p)) next.add(p);
    } else {
      for (const p of inRange) next.add(p);
    }
    selected.value = next;
  }

  function recomputeFromDrag() {
    if (!drag) return;
    const rows = opts.getRows();
    let cur = opts.indexAtY(drag.lastY);
    const anchor = drag.startIndex >= 0 ? drag.startIndex : cur >= 0 ? cur : 0;
    if (cur < 0) cur = drag.lastY < drag.startY ? 0 : rows.length - 1; // 拖出边界 clamp
    applyRange(Math.min(anchor, cur), Math.max(anchor, cur), drag.ctrl, drag.baseSet);
  }

  // ---------------- 事件 ----------------
  function onMouseDown(e: MouseEvent) {
    if (e.button !== 0) return;
    if (opts.isExcludedTarget?.(e)) return;
    const scrollEl = opts.scrollEl();
    if (!scrollEl) return;
    const tr = (e.target as HTMLElement).closest('tbody tr');
    drag = {
      startX: e.clientX,
      startY: e.clientY,
      lastX: e.clientX,
      lastY: e.clientY,
      startScrollTop: scrollEl.scrollTop,
      startIndex:
        tr && (tr as HTMLElement).dataset.idx != null
          ? Number((tr as HTMLElement).dataset.idx)
          : opts.indexAtY(e.clientY),
      ctrl: e.ctrlKey || e.metaKey,
      shift: e.shiftKey,
      baseSet: new Set(selected.value),
      moved: false,
    };
    document.addEventListener('mousemove', onDocMove, true);
    document.addEventListener('mouseup', onDocUp, true);
    scrollEl.addEventListener('scroll', onScroll, { passive: true });
    e.preventDefault(); // 阻止文本选中/拖拽
  }

  function onScroll() {
    if (drag?.moved) updateMarquee(); // 用户滚轮滚动时平移矩形
  }

  function onDocMove(e: MouseEvent) {
    if (!drag) return;
    if (!drag.moved) {
      if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) < DRAG_THRESHOLD) return;
      drag.moved = true;
      opts.onDragStart?.();
    }
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
    showMarquee();
    updateMarquee();
    recomputeFromDrag();
    autoScroll(e.clientY);
  }

  function onDocUp(e: MouseEvent) {
    const d = drag;
    document.removeEventListener('mousemove', onDocMove, true);
    document.removeEventListener('mouseup', onDocUp, true);
    opts.scrollEl()?.removeEventListener('scroll', onScroll);
    stopAutoScroll();
    drag = null;
    hideMarquee();
    if (!d) return;

    if (!d.moved) {
      // 单击语义：以落点元素判定
      const tr = (e.target as HTMLElement).closest('tbody tr') as HTMLElement | null;
      if (!tr || tr.dataset.idx == null) {
        clear(); // 单击空白/表头：清空选中
        return;
      }
      const cur = Number(tr.dataset.idx);
      const rows = opts.getRows();
      if (d.shift) {
        const anchor = anchorIndex >= 0 ? anchorIndex : cur;
        applyRange(Math.min(anchor, cur), Math.max(anchor, cur), false, null);
        anchorIndex = anchor;
      } else if (d.ctrl) {
        const p = rows[cur] && opts.proxyOf(rows[cur]);
        const next = new Set(selected.value);
        if (p) {
          if (next.has(p)) next.delete(p);
          else next.add(p);
          selected.value = next;
        }
        anchorIndex = cur;
      } else {
        const p = rows[cur] && opts.proxyOf(rows[cur]);
        selected.value = new Set(p ? [p] : []);
        anchorIndex = cur;
      }
    } else {
      anchorIndex = opts.indexAtY(e.clientY);
    }
  }

  // ---------------- 自动滚动 ----------------
  function autoScroll(clientY: number) {
    const scrollEl = opts.scrollEl();
    if (!scrollEl) return;
    const rect = scrollEl.getBoundingClientRect();
    let step = 0;
    if (clientY < rect.top + SCROLL_EDGE) step = -(SCROLL_EDGE - (clientY - rect.top));
    else if (clientY > rect.bottom - SCROLL_EDGE) step = SCROLL_EDGE - (rect.bottom - clientY);
    scrollStep = Math.max(-SCROLL_MAX, Math.min(SCROLL_MAX, step));
    if (scrollStep === 0) {
      stopAutoScroll();
      return;
    }
    if (scrollRaf) return; // 循环已在跑：只更新步长（下一帧即按新方向滚）
    const tick = () => {
      if (!drag) return;
      scrollEl.scrollTop += scrollStep;
      if (drag.moved) {
        recomputeFromDrag(); // 滚动改变行位置，按最新索引重算
        updateMarquee();
      }
      scrollRaf = requestAnimationFrame(tick);
    };
    scrollRaf = requestAnimationFrame(tick);
  }

  function stopAutoScroll() {
    if (scrollRaf) cancelAnimationFrame(scrollRaf);
    scrollRaf = null;
    scrollStep = 0;
  }

  // ---------------- 全选 / 清空 / 键盘 / 外部点击 ----------------
  function selectAll() {
    selected.value = new Set(opts.getRows().map((r) => opts.proxyOf(r)).filter(Boolean));
    anchorIndex = -1;
  }

  function clear() {
    selected.value = new Set();
    anchorIndex = -1;
  }

  function selectOne(proxy: string | null) {
    selected.value = new Set(proxy ? [proxy] : []);
    anchorIndex = -1;
  }

  function onKeyDown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      selectAll();
    } else if (e.key === 'Escape') {
      clear();
    }
  }

/** 点击容器外空白 → 清空选中；排除交互控件与浮动层（菜单/模态/框选矩形） */
function onDocMouseDown(e: MouseEvent) {
  if (e.button !== 0) return;
  const container = opts.container.value;
  if (!container || container.contains(e.target as Node)) return;
  if (
    (e.target as HTMLElement).closest(
      'button, input, select, textarea, .context-menu, .modal-overlay, .marquee-select',
    )
  )
    return;
  clear();
}

  onMounted(() => {
    opts.container.value?.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onDocMouseDown);
  });

  onBeforeUnmount(() => {
    opts.container.value?.removeEventListener('mousedown', onMouseDown);
    document.removeEventListener('keydown', onKeyDown);
    document.removeEventListener('mousedown', onDocMouseDown);
    stopAutoScroll();
    hideMarquee();
  });

  return {
    count: () => selected.value.size,
    has: (p: string) => selected.value.has(p),
    getSelectedArray: () => Array.from(selected.value),
    selectOne,
    clear,
    selectAll,
    isDragging: () => !!drag,
  };
}
