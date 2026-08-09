/**
 * 代理组页面：组选择、过滤、排序、分页、导出。
 */
(function () {
  'use strict';

  window.pageHandlers = window.pageHandlers || {};

  const PAGE_SIZE = 50;       // 每批懒加载行数
  const MAX_RENDER = 150;     // 最大同时渲染行数，超过则销毁远离视口的行
  let estRowH = 40;            // 估计行高（px），渲染后用实际值更新

  // 特殊组名中文显示映射（与仪表盘 KPI 卡片标签一致；value 仍用原组名供 API 调用）
  const GROUP_LABELS = { blacklist: '黑名单', trusted: '信任池' };
  function groupLabel(g) { return GROUP_LABELS[g] || g; }

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
      loadGroup();
    };
    document.getElementById('grp-filter').oninput = (e) => {
      state.keyword = e.target.value.trim().toLowerCase();
      state.renderStart = 0;
      state.renderEnd = 0;
      renderTable();
    };
    document.getElementById('grp-export-csv').onclick = exportCsv;
    document.getElementById('grp-export-json').onclick = exportJson;

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

  function rowTemplate(p) {
    return `
      <tr>
        <td><code>${utils.esc(p.proxy)}</code></td>
        <td>${p.consecutive_fails ?? 0}</td>
        <td>${utils.fmtLatency(p.latency)}</td>
        <td>${p.expires_in == null ? '-' : utils.fmtDuration(p.expires_in)}</td>
        <td class="text-theme-muted">${new Date((p.joined_at || 0) * 1000).toLocaleString('zh-CN')}</td>
        <td>${p.source_group ? utils.esc(groupLabel(p.source_group)) : '<span class="text-theme-muted">-</span>'}</td>
      </tr>
    `;
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
          ${renderRows.length ? renderRows.map(rowTemplate).join('') : '<tr><td colspan="6" class="text-center text-theme-muted py-8">无匹配代理</td></tr>'}
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
  }

  // 向下加载：追加底部行，超过 MAX_RENDER 时销毁顶部
  function loadMoreDown(rows) {
    const total = rows.length;
    const newEnd = Math.min(state.renderEnd + PAGE_SIZE, total);
    if (newEnd === state.renderEnd) return;
    const tbody = document.getElementById('grp-tbody');
    if (tbody) tbody.insertAdjacentHTML('beforeend', rows.slice(state.renderEnd, newEnd).map(rowTemplate).join(''));
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
  }

  // 向上加载：在顶部插入行，超过 MAX_RENDER 时销毁底部，调整 scrollTop 保持视觉位置
  function loadMoreUp(rows) {
    const newStart = Math.max(state.renderStart - PAGE_SIZE, 0);
    if (newStart === state.renderStart) return;
    const tbody = document.getElementById('grp-tbody');
    const insertCount = state.renderStart - newStart;

    // 插入行到顶部
    if (tbody) tbody.insertAdjacentHTML('afterbegin', rows.slice(newStart, state.renderStart).map(rowTemplate).join(''));
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
    const names = Object.keys(groups);
    const current = state.currentGroup || names[0];
    select.innerHTML = names.map((g) => `<option value="${utils.esc(g)}" ${g === current ? 'selected' : ''}>${utils.esc(groupLabel(g))} (${groups[g].count})</option>`).join('');

    if (state.currentGroup !== current) {
      state.currentGroup = current;
      loadGroup();
    }
  }

  window.pageHandlers.groups = { render, onStatus };
})();
