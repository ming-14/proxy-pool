/**
 * 代理组页面：组选择、过滤、排序、分页、导出。
 */
(function () {
  'use strict';

  window.pageHandlers = window.pageHandlers || {};

  const PAGE_SIZE = 50;       // 每批懒加载行数
  const MAX_RENDER = 150;     // 最大同时渲染行数，超过则销毁远离视口的行
  let estRowH = 40;            // 估计行高（px），渲染后用实际值更新

  // 组显示标签由 API 下发（groups[name].label），不再前端硬编码
  // source_group 列需要从 status.groups 查 label，缓存到 _groupLabels
  const _groupLabels = {};
  function groupLabel(g) { return _groupLabels[g] || g; }

  const page = document.getElementById('page-groups');
  let state = {
    currentGroup: null,
    detail: null,
    keyword: '',
    sortKey: 'proxy',
    sortDesc: false,
    renderStart: 0,   // 当前渲染的第一行索引（双向虚拟滚动）
    renderEnd: 0,     // 当前渲染的最后一行索引（exclusive）
  };
  let initialized = false;
  let sentinelObserver = null;  // 懒加载 IntersectionObserver（双向）
  let scrollListenerAttached = false;  // 回到顶部按钮的滚动监听是否已绑定
  let lastTotal = 0;             // 上次渲染的总行数（scroll handler 用，避免重复排序）
  let scrollJumpPending = false; // 滚动跳转重入保护
  let topSpacerH = 0;            // top spacer 实际像素高度（用实际行高累加，不用 estRowH 估算）
  let groupsCache = {};          // onStatus 缓存的 groups（右键菜单用）
  let contextMenu = null;        // 右键菜单 DOM 元素
  let selection = null;          // 行选择控制器（框选/多选）
  let currentRows = [];          // 当前过滤+排序后的行缓存（供 selection 读取，避免拖拽时重复排序）

  function render() {
    if (initialized) return;
    page.innerHTML = `
      <div class="card">
        <div class="card-header flex-wrap gap-3">
          <div class="flex items-center gap-3 flex-wrap">
            <label class="form-label mb-0">选择分组</label>
            <select id="grp-select" class="select min-w-[180px]"></select>
            <input id="grp-filter" class="input w-48" placeholder="关键字过滤">
          </div>
          <div class="flex items-center gap-2">
            <button class="btn btn-primary btn-sm" id="grp-add">添加</button>
            <button class="btn btn-ghost btn-sm" id="grp-export-csv">导出 CSV</button>
            <button class="btn btn-ghost btn-sm" id="grp-export-json">导出 JSON</button>
          </div>
        </div>
        <div class="card-body">
          <div class="table-wrap mb-4" id="grp-table-wrap">
            <div class="p-8 text-center text-theme-muted">选择一个分组查看代理明细</div>
          </div>
          <div class="text-sm text-theme-muted" id="grp-info"></div>
        </div>
      </div>
      <button id="grp-back-top" class="back-to-top-btn" style="display:none" title="回到顶部">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 15l-6-6-6 6"></path></svg>
      </button>
    `;

    document.getElementById('grp-select').onchange = (e) => {
      state.currentGroup = e.target.value;
      state.renderStart = 0;
      state.renderEnd = 0;
      if (selection) selection.clear();  // 切换分组时清空选中
      loadGroup();
    };
    document.getElementById('grp-filter').oninput = (e) => {
      state.keyword = e.target.value.trim().toLowerCase();
      state.renderStart = 0;
      state.renderEnd = 0;
      if (selection) selection.clear();  // 过滤改变视图时清空选中
      renderTable();
    };
    document.getElementById('grp-export-csv').onclick = exportCsv;
    document.getElementById('grp-export-json').onclick = exportJson;
    document.getElementById('grp-add').onclick = showAddModal;

    // 回到顶部按钮 + 虚拟滚动跳转检测：监听主区域滚动
    const mainContent = document.getElementById('main-content');
    const backTopBtn = document.getElementById('grp-back-top');
    if (!scrollListenerAttached) {
      scrollListenerAttached = true;
      mainContent.addEventListener('scroll', () => {
        if (window.appState.currentPage !== 'groups') return;
        backTopBtn.style.display = mainContent.scrollTop > 500 ? 'flex' : 'none';

        // 虚拟滚动：用户拖滚动条跳到 spacer 空白区域时，按 scrollTop 计算应渲染的行
        if (scrollJumpPending || !state.detail || lastTotal === 0) return;
        const renderedTop = state.renderStart * estRowH;
        const renderedBottom = state.renderEnd * estRowH;
        const vpTop = mainContent.scrollTop;
        const vpBottom = vpTop + mainContent.clientHeight;
        if (vpBottom < renderedTop || vpTop > renderedBottom) {
          scrollJumpPending = true;
          requestAnimationFrame(() => {
            const savedScroll = mainContent.scrollTop;
            const targetRow = Math.floor(savedScroll / estRowH);
            const newStart = Math.max(0, Math.min(targetRow, lastTotal - PAGE_SIZE));
            state.renderStart = newStart;
            state.renderEnd = Math.min(newStart + PAGE_SIZE, lastTotal);
            renderTable();
            scrollJumpPending = false;
            mainContent.scrollTop = savedScroll;
          });
        }
      });
    }
    backTopBtn.onclick = () => {
      state.renderStart = 0;
      state.renderEnd = PAGE_SIZE;
      renderTable();
      mainContent.scrollTo({ top: 0, behavior: 'smooth' });
    };

    // 右键表格行：弹出上下文菜单（移动/拉黑/还原）；右键目标行未选中时先单选它
    const tableWrap = document.getElementById('grp-table-wrap');
    tableWrap.addEventListener('contextmenu', (e) => {
      const tr = e.target.closest('tbody tr');
      if (!tr) return;
      const proxy = tr.dataset.proxy;
      if (!proxy) return;
      e.preventDefault();
      if (!selection.has(proxy)) selection.selectOne(proxy);
      showGroupContextMenu(proxy, e);
    });

    // 行选择控制器（Windows 资源管理器式框选/多选）
    // 表头 th 不排除：可从表头开始拖拽框选；th 的排序点击由 th.onclick 处理（selection 不做选择变更）
    selection = window.createRowSelection({
      container: tableWrap,
      scrollEl: document.getElementById('main-content'),
      getRows: () => currentRows,
      proxyOf: (r) => r.proxy,
      estimateIndexAtY,
      isExcludedTarget: (e) => !!e.target.closest('button, input, select'),
      onChanged: applyHighlight,
      isActive: () => window.appState.currentPage === 'groups',
    });

    initialized = true;
  }

  async function loadGroup() {
    if (!state.currentGroup) return;
    const wrap = document.getElementById('grp-table-wrap');
    wrap.innerHTML = `
      <div class="p-8 space-y-3">
        <div class="skeleton skeleton-title"></div>
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line"></div>
      </div>
    `;
    try {
      state.detail = await window.api.group(state.currentGroup);
      state.renderStart = 0;
      state.renderEnd = 0;
      renderTable();
    } catch (e) {
      wrap.innerHTML = `<div class="p-6 text-center text-theme-danger">加载失败：${utils.esc(e.message)}</div>`;
    }
  }

  function getFilteredSortedRows() {
    if (!state.detail) return [];
    let rows = state.detail.proxies.slice();
    if (state.keyword) {
      rows = rows.filter((p) => p.proxy.toLowerCase().includes(state.keyword));
    }
    rows.sort((a, b) => {
      let av, bv;
      switch (state.sortKey) {
        case 'consecutive_fails':
          av = a.consecutive_fails ?? 0;
          bv = b.consecutive_fails ?? 0;
          break;
        case 'latency':
          av = a.latency ?? Infinity;
          bv = b.latency ?? Infinity;
          break;
        case 'expires_in':
          av = a.expires_in ?? Infinity;
          bv = b.expires_in ?? Infinity;
          break;
        case 'joined_at':
          av = a.joined_at ?? 0;
          bv = b.joined_at ?? 0;
          break;
        default:
          av = a.proxy;
          bv = b.proxy;
      }
      if (av < bv) return state.sortDesc ? 1 : -1;
      if (av > bv) return state.sortDesc ? -1 : 1;
      return 0;
    });
    return rows;
  }

  function rowTemplate(p, i) {
    return `
      <tr data-idx="${i}" data-proxy="${utils.esc(p.proxy)}">
        <td><code>${utils.esc(p.proxy)}</code></td>
        <td>${p.consecutive_fails ?? 0}</td>
        <td>${utils.fmtLatency(p.latency)}</td>
        <td>${p.expires_in == null ? '-' : utils.fmtDuration(p.expires_in)}</td>
        <td class="text-theme-muted">${new Date((p.joined_at || 0) * 1000).toLocaleString('zh-CN')}</td>
        <td>${p.source_group ? utils.esc(groupLabel(p.source_group)) : '<span class="text-theme-muted">-</span>'}</td>
      </tr>
    `;
  }

  // 框选坐标 → 数据索引：tbody 顶对应 renderStart 行，按 estRowH 推算（可跨未渲染区域）
  // 表头（thead）上方 → 0，表底下方 → total-1，clamp 到有效范围
  function estimateIndexAtY(clientY) {
    if (!currentRows.length) return -1;
    const tbody = document.getElementById('grp-tbody');
    if (!tbody) return -1;
    const top = tbody.getBoundingClientRect().top;
    const idx = state.renderStart + Math.round((clientY - top) / estRowH);
    return Math.max(0, Math.min(idx, currentRows.length - 1));
  }

  // 高亮当前渲染行：根据选中集（proxy key）切换 selected class
  function applyHighlight() {
    if (!selection) return;
    const tbody = document.getElementById('grp-tbody');
    if (!tbody) return;
    for (const tr of tbody.children) {
      tr.classList.toggle('selected', selection.has(tr.dataset.proxy || ''));
    }
  }

  function renderTable() {
    if (sentinelObserver) { sentinelObserver.disconnect(); sentinelObserver = null; }
    const wrap = document.getElementById('grp-table-wrap');
    const rows = getFilteredSortedRows();
    const total = rows.length;
    lastTotal = total;  // 供 scroll handler 使用，避免重复排序

    // 初始化或重置渲染范围
    if (state.renderEnd === 0 || state.renderStart >= total) {
      state.renderStart = 0;
      state.renderEnd = Math.min(PAGE_SIZE, total);
    }
    state.renderEnd = Math.min(state.renderEnd, total);
    state.renderStart = Math.min(state.renderStart, state.renderEnd);

    const renderRows = rows.slice(state.renderStart, state.renderEnd);
    currentRows = rows;  // 缓存供 selection 读取（避免拖拽/框选时重复排序）
    const hasMoreDown = state.renderEnd < total;
    topSpacerH = state.renderStart * estRowH;  // 初始估算（scroll jump 或首次渲染）
    let bottomSpacerH = (total - state.renderEnd) * estRowH;

    const sortIcon = (key) => {
      if (state.sortKey !== key) return '<span class="sort-icon">↕</span>';
      return `<span class="sort-icon">${state.sortDesc ? '↓' : '↑'}</span>`;
    };

    wrap.innerHTML = `
      <div id="grp-top-spacer" style="height:${topSpacerH}px"></div>
      <table>
        <thead>
          <tr>
            <th class="sortable" data-sort="proxy">代理 ${sortIcon('proxy')}</th>
            <th class="sortable" data-sort="consecutive_fails">连续失败 ${sortIcon('consecutive_fails')}</th>
            <th class="sortable" data-sort="latency">延迟 ${sortIcon('latency')}</th>
            <th class="sortable" data-sort="expires_in">过期倒计时 ${sortIcon('expires_in')}</th>
            <th class="sortable" data-sort="joined_at">加入时间 ${sortIcon('joined_at')}</th>
            <th class="sortable" data-sort="source_group">来源 ${sortIcon('source_group')}</th>
          </tr>
        </thead>
        <tbody id="grp-tbody">
          ${renderRows.length ? renderRows.map((p, i) => rowTemplate(p, state.renderStart + i)).join('') : '<tr><td colspan="6" class="text-center text-theme-muted py-8">无匹配代理</td></tr>'}
        </tbody>
      </table>
      ${hasMoreDown ? '<div id="grp-sentinel" class="text-center text-theme-muted py-4">滚动加载更多...</div>' : (total > 0 ? '<div class="text-center text-theme-muted py-3 text-xs">已全部加载</div>' : '')}
      <div id="grp-bottom-spacer" style="height:${bottomSpacerH}px"></div>
    `;

    wrap.querySelectorAll('th.sortable').forEach((th) => {
      th.onclick = () => {
        const key = th.dataset.sort;
        if (state.sortKey === key) {
          state.sortDesc = !state.sortDesc;
        } else {
          state.sortKey = key;
          state.sortDesc = false;
        }
        state.renderStart = 0;
        state.renderEnd = PAGE_SIZE;
        renderTable();
      };
    });

    updateInfo(rows);

    // 测量实际行高（含 border-spacing），更新估计值
    const tbody = document.getElementById('grp-tbody');
    if (tbody && tbody.children.length > 0 && tbody.offsetHeight > 0) {
      estRowH = tbody.offsetHeight / tbody.children.length;
      // 全量重渲染：topSpacerH 用新 estRowH 重算（估算未渲染行的高度）
      topSpacerH = state.renderStart * estRowH;
      const renderedH = tbody.offsetHeight;
      const totalH = total * estRowH;
      const topSpacer = document.getElementById('grp-top-spacer');
      const bottomSpacer = document.getElementById('grp-bottom-spacer');
      if (topSpacer) topSpacer.style.height = topSpacerH + 'px';
      if (bottomSpacer) bottomSpacer.style.height = Math.max(0, totalH - topSpacerH - renderedH) + 'px';
    }

    // 双向懒加载：底部 sentinel 向下加载，顶部占位符向上加载
    // root 必须设为 #main-content（滚动容器），否则默认用浏览器视口，滚动时不触发
    const scrollRoot = document.getElementById('main-content');
    sentinelObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        if (entry.target.id === 'grp-sentinel') loadMoreDown(rows);
        else if (entry.target.id === 'grp-top-spacer' && state.renderStart > 0) loadMoreUp(rows);
      }
    }, { root: scrollRoot, rootMargin: '300px' });
    const sentinel = document.getElementById('grp-sentinel');
    if (sentinel) sentinelObserver.observe(sentinel);
    if (state.renderStart > 0) sentinelObserver.observe(document.getElementById('grp-top-spacer'));

    if (selection) selection.refresh();  // 重渲染后重放高亮
  }

  // 向下加载：追加底部行，超过 MAX_RENDER 时销毁顶部
  function loadMoreDown(rows) {
    const total = rows.length;
    const newEnd = Math.min(state.renderEnd + PAGE_SIZE, total);
    if (newEnd === state.renderEnd) return;
    const tbody = document.getElementById('grp-tbody');
    if (tbody) tbody.insertAdjacentHTML('beforeend', rows.slice(state.renderEnd, newEnd).map((p, i) => rowTemplate(p, state.renderEnd + i)).join(''));
    state.renderEnd = newEnd;

    // trim 顶部行：topSpacerH 用实际移除高度累加，spacer 增量 = removedH，净位移 = 0
    const rendered = state.renderEnd - state.renderStart;
    if (rendered > MAX_RENDER && tbody) {
      const trim = rendered - MAX_RENDER;
      let removedH = 0;
      for (let i = 0; i < trim && tbody.firstChild; i++) {
        removedH += tbody.firstChild.offsetHeight || estRowH;
        tbody.removeChild(tbody.firstChild);
      }
      state.renderStart += trim;
      topSpacerH += removedH;
    }
    updateSpacers(rows);
    updateInfo(rows);
    if (selection) selection.refresh();
  }

  // 向上加载：在顶部插入行，超过 MAX_RENDER 时销毁底部，调整 scrollTop 保持视觉位置
  function loadMoreUp(rows) {
    const newStart = Math.max(state.renderStart - PAGE_SIZE, 0);
    if (newStart === state.renderStart) return;
    const tbody = document.getElementById('grp-tbody');
    const insertCount = state.renderStart - newStart;

    // 插入行到顶部
    if (tbody) tbody.insertAdjacentHTML('afterbegin', rows.slice(newStart, state.renderStart).map((p, i) => rowTemplate(p, newStart + i)).join(''));
    state.renderStart = newStart;

    // 测量插入行实际高度，topSpacerH 用实际高度递减，spacer 减量 = insertedH，净位移 = 0
    let insertedH = 0;
    if (tbody) {
      for (let i = 0; i < insertCount && i < tbody.children.length; i++) insertedH += tbody.children[i].offsetHeight;
    }
    topSpacerH = Math.max(0, topSpacerH - insertedH);

    // trim 底部（不影响 scrollTop）
    const rendered = state.renderEnd - state.renderStart;
    if (rendered > MAX_RENDER && tbody) {
      const trim = rendered - MAX_RENDER;
      for (let i = 0; i < trim && tbody.lastChild; i++) tbody.removeChild(tbody.lastChild);
      state.renderEnd -= trim;
    }
    updateSpacers(rows);
    updateInfo(rows);
    if (selection) selection.refresh();
  }

  function updateSpacers(rows) {
    const total = rows.length;
    const top = document.getElementById('grp-top-spacer');
    const bot = document.getElementById('grp-bottom-spacer');
    const tbodyEl = document.getElementById('grp-tbody');
    const renderedH = tbodyEl ? tbodyEl.offsetHeight : (state.renderEnd - state.renderStart) * estRowH;
    const totalH = total * estRowH;
    const botH = Math.max(0, totalH - topSpacerH - renderedH);
    if (top) top.style.height = topSpacerH + 'px';
    if (bot) bot.style.height = botH + 'px';
    if (state.renderEnd >= total) {
      const s = document.getElementById('grp-sentinel');
      if (s) s.outerHTML = '<div class="text-center text-theme-muted py-3 text-xs">已全部加载</div>';
    }
  }

  function updateInfo(rows) {
    const total = rows.length;
    document.getElementById('grp-info').textContent = `共 ${total} 条 · 显示第 ${state.renderStart + 1}-${state.renderEnd} 条`;
  }

  function exportCsv() {
    const rows = getFilteredSortedRows();
    if (!rows.length) {
      showToast('warning', '无数据', '当前没有可导出的代理');
      return;
    }
    const header = ['proxy', 'consecutive_fails', 'latency', 'expires_in', 'joined_at', 'source_group'];
    const lines = rows.map((p) => [
      p.proxy,
      p.consecutive_fails ?? 0,
      p.latency ?? '',
      p.expires_in ?? '',
      new Date((p.joined_at || 0) * 1000).toISOString(),
      p.source_group ?? '',
    ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
    downloadFile([header.join(','), ...lines].join('\n'), `${state.currentGroup}.csv`, 'text/csv');
  }

  function exportJson() {
    const rows = getFilteredSortedRows();
    if (!rows.length) {
      showToast('warning', '无数据', '当前没有可导出的代理');
      return;
    }
    downloadFile(JSON.stringify(rows, null, 2), `${state.currentGroup}.json`, 'application/json');
  }

  function downloadFile(content, filename, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function onStatus(status) {
    if (window.appState.currentPage !== 'groups') return;
    const select = document.getElementById('grp-select');
    if (!select) return;

    const groups = status.groups || {};
    groupsCache = groups;  // 缓存供右键菜单用
    const names = Object.keys(groups);
    // 缓存 API 下发的 label 供 source_group 列使用
    for (const g of names) _groupLabels[g] = groups[g].label || g;
    const current = state.currentGroup || names[0];
    select.innerHTML = names.map((g) => `<option value="${utils.esc(g)}" ${g === current ? 'selected' : ''}>${utils.esc(_groupLabels[g])} (${groups[g].count})</option>`).join('');

    if (state.currentGroup !== current) {
      state.currentGroup = current;
      loadGroup();
    }
  }

  // ---------------- 右键上下文菜单：移动（子菜单）/ 拉黑（非黑名单组）/ 还原（黑名单组）----------------
  function showGroupContextMenu(proxy, mouseEvent) {
    hideGroupContextMenu();
    const currentKind = groupsCache[state.currentGroup]?.kind || 'normal';

    // 移出子菜单：列出所有组（排除当前组和黑名单——黑名单用"还原"项）
    const moveItems = Object.entries(groupsCache)
      .filter(([name, g]) => name !== state.currentGroup && g.kind !== 'blacklist')
      .map(([name, g]) => `
        <div class="context-menu-item" data-action="move" data-group="${utils.esc(name)}">
          <span>${utils.esc(g.label)}</span>
          <span class="text-theme-muted text-xs">${g.count}</span>
        </div>
      `).join('');

    // 黑名单组：显示"还原"+"删除"，非黑名单组：显示"移动"+"拉黑"+"删除"
    let items = '';
    const deleteItem = `<div class="context-menu-item context-menu-danger" data-action="delete"><span>删除</span></div>`;
    if (currentKind === 'blacklist') {
      items = `
        <div class="context-menu-item" data-action="restore"><span>还原</span></div>
        ${deleteItem}
      `;
    } else {
      items = `
        <div class="context-menu-item context-menu-has-submenu">
          <span>移动</span>
          <svg class="context-menu-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
          <div class="context-submenu">${moveItems}</div>
        </div>
        <div class="context-menu-item" data-action="blacklist"><span>拉黑</span></div>
        ${deleteItem}
      `;
    }

    contextMenu = document.createElement('div');
    contextMenu.className = 'context-menu';
    contextMenu.innerHTML = items;
    document.body.appendChild(contextMenu);
    positionGroupContextMenu(mouseEvent);

    contextMenu.addEventListener('click', (e) => {
      const item = e.target.closest('.context-menu-item');
      if (!item || !item.dataset.action) return;
      const action = item.dataset.action;
      if (action === 'move') {
        doGroupMove(proxy, item.dataset.group);
        hideGroupContextMenu();
      } else if (action === 'blacklist') {
        doGroupBlacklist(proxy);
        hideGroupContextMenu();
      } else if (action === 'restore') {
        doGroupRestore(proxy);
        hideGroupContextMenu();
      } else if (action === 'delete') {
        doGroupDelete(proxy);
        hideGroupContextMenu();
      }
    });

    document.addEventListener('click', hideGroupContextMenuOnOutside, true);
    document.addEventListener('scroll', hideGroupContextMenu, true);
    window.addEventListener('resize', hideGroupContextMenu);
  }

  function hideGroupContextMenuOnOutside(e) {
    if (contextMenu && !contextMenu.contains(e.target)) hideGroupContextMenu();
  }

  function hideGroupContextMenu() {
    if (contextMenu) { contextMenu.remove(); contextMenu = null; }
    document.removeEventListener('click', hideGroupContextMenuOnOutside, true);
    document.removeEventListener('scroll', hideGroupContextMenu, true);
    window.removeEventListener('resize', hideGroupContextMenu);
  }

  function positionGroupContextMenu(e) {
    contextMenu.style.visibility = 'hidden';
    requestAnimationFrame(() => {
      const rect = contextMenu.getBoundingClientRect();
      const maxX = window.innerWidth - rect.width - 8;
      const maxY = window.innerHeight - rect.height - 8;
      contextMenu.style.left = Math.min(Math.max(e.clientX, 8), maxX) + 'px';
      contextMenu.style.top = Math.min(Math.max(e.clientY, 8), maxY) + 'px';
      contextMenu.style.visibility = '';
    });
  }

  // 当前选中集（右键菜单批量操作作用于整个选中集；空则用右键目标行兜底）
  function selectedProxies() {
    const sel = selection ? selection.getSelectedArray() : [];
    return sel.length ? sel : null;
  }

  async function doGroupMove(proxy, targetGroup) {
    const proxies = selectedProxies() || [proxy];
    try {
      const d = await window.api.moveProxy(proxies, targetGroup);
      const label = groupsCache[targetGroup]?.label || targetGroup;
      showToast('success', '已移动', `已将 ${d.count} 个代理移到 ${label}`);
      selection && selection.clear();
      loadGroup();  // 刷新当前组明细
    } catch (e) {
      showToast('error', '移动失败', e.message);
    }
  }

  async function doGroupBlacklist(proxy) {
    const proxies = selectedProxies() || [proxy];
    try {
      const d = await window.api.blacklist(proxies);
      showToast('success', '已拉黑', `已将 ${d.count} 个代理加入黑名单`);
      selection && selection.clear();
      loadGroup();
    } catch (e) {
      showToast('error', '拉黑失败', e.message);
    }
  }

  async function doGroupRestore(proxy) {
    // 还原：回到拉黑前的来源组（source_group 由后端记入黑名单 info），无来源组记录（直接入黑名单的代理）报错
    const proxies = selectedProxies() || [proxy];
    // 按 source_group 分组批量移动（各自回到来源组）
    const byTarget = {};
    let skipped = 0;
    for (const p of proxies) {
      const entry = state.detail?.proxies.find((x) => x.proxy === p);
      const target = entry?.source_group;
      if (!target) { skipped++; continue; }
      (byTarget[target] = byTarget[target] || []).push(p);
    }
    if (!Object.keys(byTarget).length) {
      showToast('error', '还原失败', '所选代理均无来源组记录，无法还原');
      return;
    }
    try {
      let total = 0;
      for (const [target, list] of Object.entries(byTarget)) {
        const d = await window.api.moveProxy(list, target);
        total += d.count;
      }
      const skippedText = skipped ? `，${skipped} 个无来源组被跳过` : '';
      showToast('success', '已还原', `已还原 ${total} 个代理${skippedText}`);
      selection && selection.clear();
      loadGroup();
    } catch (e) {
      showToast('error', '还原失败', e.message);
    }
  }

  async function doGroupDelete(proxy) {
    const proxies = selectedProxies() || [proxy];
    try {
      const d = await window.api.removeProxy(proxies, state.currentGroup);
      showToast('success', '已删除', `已从当前组移除 ${d.count} 个代理`);
      selection && selection.clear();
      loadGroup();
    } catch (e) {
      showToast('error', '删除失败', e.message);
    }
  }

  // ---------------- 添加代理模态框 ----------------
  let addModal = null;

  function showAddModal() {
    if (!state.currentGroup) {
      showToast('warning', '请先选择分组', '选择要添加代理的目标组');
      return;
    }
    if (addModal) return;
    const label = groupsCache[state.currentGroup]?.label || state.currentGroup;

    addModal = document.createElement('div');
    addModal.className = 'modal-overlay';
    addModal.innerHTML = `
      <div class="modal">
        <div class="modal-header">
          <div class="modal-title">添加代理到 ${utils.esc(label)}</div>
          <button class="btn btn-ghost btn-sm" id="grp-add-close">&times;</button>
        </div>
        <div class="modal-body">
          <textarea class="input w-full" id="grp-add-input" rows="8" placeholder="每行一个代理地址，如&#10;http://1.2.3.4:8080&#10;socks5://5.6.7.8:1080&#10;9.9.9.9:3128（裸地址默认 http）"></textarea>
        </div>
        <div class="modal-footer">
          <button class="btn btn-ghost btn-sm" id="grp-add-cancel">取消</button>
          <button class="btn btn-primary btn-sm" id="grp-add-submit">添加</button>
        </div>
      </div>
    `;
    document.body.appendChild(addModal);

    const close = () => { addModal.remove(); addModal = null; };
    document.getElementById('grp-add-close').onclick = close;
    document.getElementById('grp-add-cancel').onclick = close;
    addModal.addEventListener('click', (e) => { if (e.target === addModal) close(); });
    document.getElementById('grp-add-submit').onclick = () => submitAdd(close);
    document.getElementById('grp-add-input').focus();
  }

  async function submitAdd(close) {
    const input = document.getElementById('grp-add-input');
    const proxies = input.value.split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean);
    if (!proxies.length) {
      showToast('warning', '无代理', '请输入至少一个代理地址');
      return;
    }
    const btn = document.getElementById('grp-add-submit');
    btn.disabled = true;
    btn.textContent = '添加中...';
    try {
      const d = await window.api.addProxy(state.currentGroup, proxies);
      showToast('success', '已添加', `成功添加 ${d.added} 个代理`);
      close();
      loadGroup();
    } catch (e) {
      showToast('error', '添加失败', e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '添加';
    }
  }

  window.pageHandlers.groups = { render, onStatus };
})();
