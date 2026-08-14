/**
 * 通用表格行选择模块（Windows 资源管理器式框选 / 多选）
 *
 * 交互语义（对齐资源管理器）：
 * - 空白处或行上按下鼠标拖拽 → 蓝色框选矩形，矩形覆盖的行被选中
 * - 单击行 → 单选；Ctrl+单击 → 切换该行；Shift+单击 → 锚点范围选择
 * - Ctrl+拖拽 → 切换框内行（框外原有选中保留）
 * - Ctrl+A → 全选当前列表；Esc → 清空选中；单击空白 → 清空选中
 * - 拖到滚动容器顶部/底部边缘 → 自动滚动（框选可跨未渲染区域）
 *
 * 与页面解耦：通过回调注入行数据 / 坐标→索引估计 / 高亮刷新，
 * 同时兼容虚拟滚动（groups）与全渲染（standby）两种列表。
 *
 * 约定：渲染行 <tr> 需带 data-idx（数据索引）与 data-proxy（代理字符串）。
 */
(function () {
  'use strict';

  const DRAG_THRESHOLD = 4;  // 判定进入拖拽的位移阈值（px）
  const SCROLL_EDGE = 40;    // 触发自动滚动的边缘距离（px）
  const SCROLL_MAX = 30;     // 自动滚动每帧最大步长（px）

  /**
   * 创建一行选择控制器。
   *
   * opts:
   *   container         : 鼠标事件绑定容器（.table-wrap 元素）
   *   scrollEl          : 滚动容器（#main-content）
   *   getRows           : () => 当前过滤/排序后的行数组
   *   proxyOf           : (row) => 代理字符串
   *   estimateIndexAtY  : (clientY) => 按几何位置估计的数据索引（跨未渲染区域），
   *                        -1 表示不在列表范围内
   *   isExcludedTarget  : (e) => 是否禁止从此目标开始交互（如表头/按钮）
   *   onChanged         : (selectedSet) => 选中集合变化回调（应用行高亮）
   *   isActive          : () => 当前页面是否激活（仅激活页响应 Ctrl+A/Esc）
   */
  window.createRowSelection = function (opts) {
    const state = {
      selected: new Set(),
      anchorIndex: -1,   // Shift 范围选择 / 单选锚点
      drag: null,        // 拖拽状态（null 表示未拖拽）
      marquee: null,     // 框选矩形 DOM
      scrollRaf: null,   // 自动滚动 rAF
    };

    // ---------------- 框选矩形 ----------------
    function showMarquee() {
      if (state.marquee) return;
      const el = document.createElement('div');
      el.className = 'marquee-select';
      document.body.appendChild(el);
      state.marquee = el;
    }

    function updateMarquee() {
      const d = state.drag;
      if (!d || !state.marquee) return;
      state.marquee.style.left = Math.min(d.startX, d.lastX) + 'px';
      state.marquee.style.top = Math.min(d.startY, d.lastY) + 'px';
      state.marquee.style.width = Math.abs(d.lastX - d.startX) + 'px';
      state.marquee.style.height = Math.abs(d.lastY - d.startY) + 'px';
    }

    function hideMarquee() {
      if (state.marquee) { state.marquee.remove(); state.marquee = null; }
    }

    // ---------------- 选中计算 ----------------
    // 将 [lo, hi] 索引区间应用到选中集合；ctrl 时切换框内（保留框外 baseSet），否则仅框内
    function applyRange(rows, lo, hi, ctrl, baseSet) {
      const total = rows.length;
      lo = Math.max(0, lo);
      hi = Math.min(total - 1, hi);
      const inRange = new Set();
      for (let i = lo; i <= hi; i++) {
        const p = rows[i] && opts.proxyOf(rows[i]);
        if (p) inRange.add(p);
      }
      const next = new Set();
      if (ctrl && baseSet) {
        for (const p of baseSet) if (!inRange.has(p)) next.add(p);
        for (const p of inRange) if (!baseSet.has(p)) next.add(p);
      } else {
        for (const p of inRange) next.add(p);
      }
      state.selected = next;
      opts.onChanged && opts.onChanged(state.selected);
    }

    // 根据当前拖拽状态重算选中（自动滚动后索引变化时调用）
    function recomputeFromDrag() {
      const d = state.drag;
      if (!d) return;
      const rows = opts.getRows();
      let cur = opts.estimateIndexAtY(d.lastY);
      const anchor = d.startIndex >= 0 ? d.startIndex : (cur >= 0 ? cur : 0);
      if (cur < 0) cur = d.lastY < d.startY ? 0 : rows.length - 1;  // 拖出边界时 clamp
      applyRange(rows, Math.min(anchor, cur), Math.max(anchor, cur), d.ctrl, d.baseSet);
    }

    // ---------------- 事件处理 ----------------
    function onMouseDown(e) {
      if (e.button !== 0) return;
      if (opts.isExcludedTarget && opts.isExcludedTarget(e)) return;
      const tr = e.target.closest('tbody tr');
      state.drag = {
        startX: e.clientX, startY: e.clientY,
        lastX: e.clientX, lastY: e.clientY,
        startIndex: tr && tr.dataset.idx != null ? Number(tr.dataset.idx) : opts.estimateIndexAtY(e.clientY),
        ctrl: e.ctrlKey || e.metaKey,
        shift: e.shiftKey,
        baseSet: new Set(state.selected),
        moved: false,
      };
      document.addEventListener('mousemove', onDocMove, true);
      document.addEventListener('mouseup', onDocUp, true);
      e.preventDefault();  // 阻止文本选中 / 拖拽
    }

    function onDocMove(e) {
      const d = state.drag;
      if (!d) return;
      if (!d.moved && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < DRAG_THRESHOLD) return;
      d.moved = true;
      d.lastX = e.clientX;
      d.lastY = e.clientY;
      showMarquee();
      updateMarquee();
      recomputeFromDrag();
      autoScroll(e.clientY);
    }

    function onDocUp(e) {
      const d = state.drag;
      document.removeEventListener('mousemove', onDocMove, true);
      document.removeEventListener('mouseup', onDocUp, true);
      stopAutoScroll();
      state.drag = null;
      hideMarquee();
      if (!d) return;

      if (!d.moved) {
        // 单击语义（未拖动）：以鼠标落点元素判定
        const tr = e.target.closest('tbody tr');
        if (!tr || tr.dataset.idx == null) {
          // 单击空白 / 表头：清空选中（表头排序由 th.onclick 处理，不影响清空）
          state.selected.clear();
          state.anchorIndex = -1;
          opts.onChanged && opts.onChanged(state.selected);
          return;
        }
        const cur = Number(tr.dataset.idx);
        const rows = opts.getRows();
        if (d.shift) {
          // Shift+单击：从锚点范围选择
          const anchor = state.anchorIndex >= 0 ? state.anchorIndex : cur;
          applyRange(rows, Math.min(anchor, cur), Math.max(anchor, cur), false, null);
          state.anchorIndex = anchor;
        } else if (d.ctrl) {
          // Ctrl+单击：切换该行
          const p = rows[cur] && opts.proxyOf(rows[cur]);
          if (p) {
            if (state.selected.has(p)) state.selected.delete(p);
            else state.selected.add(p);
            opts.onChanged && opts.onChanged(state.selected);
          }
          state.anchorIndex = cur;
        } else {
          // 普通单击：单选
          const p = rows[cur] && opts.proxyOf(rows[cur]);
          state.selected = new Set(p ? [p] : []);
          state.anchorIndex = cur;
          opts.onChanged && opts.onChanged(state.selected);
        }
      } else {
        state.anchorIndex = opts.estimateIndexAtY(e.clientY);
      }
    }

    // ---------------- 自动滚动 ----------------
    function autoScroll(clientY) {
      const rect = opts.scrollEl.getBoundingClientRect();
      let step = 0;
      if (clientY < rect.top + SCROLL_EDGE) step = -(SCROLL_EDGE - (clientY - rect.top));
      else if (clientY > rect.bottom - SCROLL_EDGE) step = SCROLL_EDGE - (rect.bottom - clientY);
      step = Math.max(-SCROLL_MAX, Math.min(SCROLL_MAX, step));
      if (step === 0) { stopAutoScroll(); return; }
      if (state.scrollRaf) return;
      state.scrollRaf = requestAnimationFrame(function tick() {
        opts.scrollEl.scrollTop += step;
        if (state.drag && state.drag.moved) {
          recomputeFromDrag();  // 滚动后行位置变化，按最新索引重算
          updateMarquee();
        }
        state.scrollRaf = requestAnimationFrame(tick);
      });
    }

    function stopAutoScroll() {
      if (state.scrollRaf) { cancelAnimationFrame(state.scrollRaf); state.scrollRaf = null; }
    }

    // ---------------- 全选 / 清空 / 键盘 ----------------
    function selectAll() {
      const rows = opts.getRows();
      state.selected = new Set(rows.map((r) => opts.proxyOf(r)).filter(Boolean));
      state.anchorIndex = -1;
      opts.onChanged && opts.onChanged(state.selected);
    }

    function clearSelection() {
      state.selected.clear();
      state.anchorIndex = -1;
      opts.onChanged && opts.onChanged(state.selected);
    }

    function onKeyDown(e) {
      if (opts.isActive && !opts.isActive()) return;
      const tag = e.target && e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
        e.preventDefault();
        selectAll();
      } else if (e.key === 'Escape') {
        clearSelection();
      }
    }

    // 点击容器外部空白（页面其它区域）→ 清空选中（对齐资源管理器）
    // 排除：容器内（由 onMouseDown 处理）、可交互控件（按钮/输入框/下拉）、
    // 浮动层（右键菜单/tooltip/模态框）——这些区域的点击不应清空选中
    function onDocMouseDown(e) {
      if (e.button !== 0) return;
      if (opts.isActive && !opts.isActive()) return;
      if (opts.container.contains(e.target)) return;
      if (e.target.closest('button, input, select, textarea, '
          + '.context-menu, .context-menu *, .proxy-tooltip, .proxy-tooltip *, '
          + '.modal-overlay, .modal-overlay *, .marquee-select')) return;
      clearSelection();
    }

    // ---------------- 挂载 ----------------
    opts.container.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onDocMouseDown);

    // 控制器：供页面调用（右键菜单 / 高亮刷新）
    return {
      has(p) { return state.selected.has(p); },
      count() { return state.selected.size; },
      getSelectedArray() { return Array.from(state.selected); },
      getSelectedSet() { return state.selected; },
      selectOne(proxy) {
        state.selected = new Set(proxy ? [proxy] : []);
        state.anchorIndex = -1;
        opts.onChanged && opts.onChanged(state.selected);
      },
      clear: clearSelection,
      selectAll,
      refresh() { opts.onChanged && opts.onChanged(state.selected); },  // 列表重渲染后重放高亮
      isDragging() { return !!state.drag; },  // 是否正在框选拖拽（供页面抑制其他悬浮交互）
    };
  };
})();
