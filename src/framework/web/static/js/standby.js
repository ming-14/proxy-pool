/**
 * 代理服务队列页面：队列明细、待补测代理、调度参数。
 */
(function () {
  'use strict';

  window.pageHandlers = window.pageHandlers || {};

  const page = document.getElementById('page-standby');
  let initialized = false;
  let settingsBaseline = null;  // 调度参数基线快照，用于检测是否有改动（控制保存按钮启停）

  // hover tooltip 状态：鼠标悬浮代理 IP 时展示该 IP 的延时+流量图表
  let hoverTooltip = null;   // tooltip DOM 元素（挂在 document.body，不受表格重绘影响）
  let hoverCharts = null;    // charts.createProxyCharts 返回的实例
  let hoverBind = null;      // 当前 hover 的 bind（地址，不含 scheme）
  let hoverTimer = null;     // 数据刷新定时器（2s 间隔，与 app.js 轮询同频）
  let hideTimer = null;      // 延迟销毁定时器（mouseleave 后 100ms 销毁，期间若重绘触发 mouseover 则取消）
  let hoverShowTimer = null;  // 延迟显示定时器（悬停 300ms 才显示 tooltip，避免划过误触发）
  let hoverSource = null;    // tooltip 来源：'table'（表格）或 'lock'（锁定卡片），renderTable 检查只针对表格
  let speedTestState = null; // 测速状态：{status:'running'|'done', bind, result, time}，tooltip 隐藏后保留，重建时恢复
  let standbyGroups = {};    // onStatus 缓存的 groups（右键菜单移出子菜单用）
  let contextMenu = null;    // 右键菜单 DOM 元素
  let tooltipPinned = false; // 测速 pin：true 时 tooltip 不自动消失，仅点击外部才消失
  let selection = null;      // 行选择控制器（框选/多选）
  let currentStandby = [];   // 当前队列数组（供 selection 读取，与渲染保持一致）
  let hoveredProxy = null;   // 当前鼠标悬浮的行代理（hover 时冻结排序，避免悬浮行跑位）
  let selectingText = false; // 用户正在拖选 code 文本（按下到松开期间，跳过重绘避免打断选区）
  let selectingTextChangeHandler = null;  // selectionchange 监听器（选区消失时重置 selectingText）
  let lockSelectingText = false;  // 锁定卡片 IP 文本选择中（与 selectingText 同机制：选区存在时冻结锁定卡片 DOM 更新）
  let lockSelectingTextChangeHandler = null;  // 锁定卡片 selectionchange 监听器（选区消失时重置 lockSelectingText）

  // AbuseIPDB 纯洁度检查结果缓存：bind → {status:'checking'|'done'|'error', abuse_confidence_score, usage_type, ...}
  // 表格每 2s 重绘，缓存让 tag 在重绘后保留；页面刷新后清空（按需重新检查）
  const purityCache = new Map();

  // usageType 缩短映射（AbuseIPDB + ipok.io 返回的英文 → 中文短标签，tag 空间有限）
  const USAGE_TYPE_SHORT = {
    'Commercial': '商业',
    'Organization': '组织',
    'Government': '政府',
    'Military': '军事',
    'University/College/School': '学校',
    'Library': '图书馆',
    'Content Delivery Network': 'CDN',
    'Fixed Line ISP': '固定ISP',
    'Mobile ISP': '移动ISP',
    'Data Center/Web Hosting/Transit': '数据中心',
    'Search Engine Spider': '爬虫',
    'Reserved': '保留',
    'hosting': '托管',
    'residential': '住宅',
    'mobile': '移动',
    'business': '企业',
    'unknown': '未知',
  };

  // 纯洁度 tag：abuseConfidenceScore（按分值着色）+ usageType（缩短中文）
  // data 来源：purityCache（手动检查，含 status）或后端 status 快照的 p.purity（无 status）
  function renderPurityTags(data) {
    if (!data) return '';
    if (data.status === 'checking' || data.status === 'error') return '';
    const score = data.abuse_confidence_score;
    if (score == null) return '';
    const scoreCls = score === 0 ? 'badge-success' : score < 25 ? 'badge-muted' : score < 75 ? 'badge-warning' : 'badge-danger';
    const usageFull = data.usage_type || '';
    const usageShort = USAGE_TYPE_SHORT[usageFull] || usageFull || '-';
    return `<span class="badge ${scoreCls} ml-2" title="滥用置信度 ${score}/100">${score}</span><span class="badge badge-info ml-1" title="${utils.esc(usageFull)}">${utils.esc(usageShort)}</span>`;
  }

  // 纯洁度按钮：检查中显示禁用态，否则显示"纯洁"
  function purityBtnHtml(proxy) {
    const bind = utils.bindOf(proxy);
    const r = purityCache.get(bind);
    if (r && r.status === 'checking') {
      return `<button class="btn btn-ghost btn-sm" data-action="purity" data-proxy="${utils.esc(proxy)}" disabled>检查中</button>`;
    }
    return `<button class="btn btn-ghost btn-sm" data-action="purity" data-proxy="${utils.esc(proxy)}">纯洁</button>`;
  }

  // 高亮当前渲染行：根据选中集（proxy key）切换 selected class
  function applyHighlight() {
    if (!selection) return;
    const tbody = document.querySelector('#sb-table-wrap tbody');
    if (!tbody) return;
    for (const tr of tbody.children) {
      tr.classList.toggle('selected', selection.has(tr.dataset.proxy || ''));
    }
  }

  function render() {
    if (initialized) return;
    page.innerHTML = `
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div class="lg:col-span-2 space-y-5">
          <div class="card border-theme-warning" id="sb-lock-card" style="display:none">
            <div class="card-header">
              <div>
                <div class="card-title">已锁定单一上游</div>
                <div class="card-subtitle">无论心跳是否成功，只使用此代理</div>
              </div>
            </div>
            <div class="card-body flex items-center justify-between">
              <div class="flex items-center gap-2 min-w-0">
                <code id="sb-lock-proxy" class="text-sm font-medium"></code>
                <span id="sb-lock-purity-tags" class="flex items-center gap-1 flex-shrink-0"></span>
              </div>
              <div class="flex items-center gap-2 flex-shrink-0">
                <button class="btn btn-ghost btn-sm" id="sb-lock-purity-btn">纯洁</button>
                <button class="btn btn-warning btn-sm" id="sb-unlock-btn">解锁</button>
              </div>
            </div>
          </div>
          <div class="card" id="sb-queue-card">
            <div class="card-header">
              <div>
                <div class="card-title">代理服务队列</div>
                <div class="card-subtitle">pproxy 队列</div>
              </div>
            </div>
            <div class="table-wrap border-0 rounded-t-none" id="sb-table-wrap">
              <div class="empty-state">暂无数据</div>
            </div>
          </div>
        </div>

        <div class="space-y-5">
          <div class="card">
            <div class="card-header"><div class="card-title">队列统计</div></div>
            <div class="card-body space-y-3" id="sb-stats"></div>
          </div>
          <div class="card" id="sb-settings-card">
            <div class="card-header"><div class="card-title">调度参数</div></div>
            <div class="card-body space-y-3 text-sm" id="sb-settings"></div>
          </div>
          <div class="card" id="sb-pending-card" style="display:none">
            <div class="card-header"><div class="card-title">待补测代理</div></div>
            <div class="card-body text-sm" id="sb-pending"></div>
          </div>
        </div>
      </div>
    `;

    initialized = true;

    // 代理列 hover 事件委托：mouseover 冒泡检测进入 code，mouseleave 离开容器时销毁
    // 用 mouseleave 而非 mouseout：表格每 2s innerHTML 重绘会触发 mouseout（旧 DOM 移除），
    // 但 mouseleave 只在鼠标真正离开容器时触发，重绘不误触
    const wrap = document.getElementById('sb-table-wrap');
    wrap.addEventListener('mouseover', (e) => {
      // 追踪当前悬浮行（用于冻结排序，避免 2s 刷新重排导致悬浮行跑位）
      const tr = e.target.closest('tbody tr');
      hoveredProxy = tr ? (tr.dataset.proxy || null) : null;
      if (selection && selection.isDragging()) { cancelShowProxyTooltip(); return; }  // 框选拖拽时抑制 hover tooltip
      const code = e.target.closest('code');
      if (!code) { cancelShowProxyTooltip(); return; }
      if (window.getSelection().toString()) { cancelShowProxyTooltip(); return; }  // 有文本选区时不显示悬浮面板
      const bind = utils.bindOf(code.textContent);
      if (hoverBind === bind) return;
      scheduleShowProxyTooltip(bind, e, 'table');
    });
    wrap.addEventListener('mouseleave', () => {
      hoveredProxy = null;
      cancelShowProxyTooltip();
      scheduleHideProxyTooltip();
    });

    // 锁定卡片代理 IP：hover 不自动弹出统计浮窗（fixed 浮窗会遮住下方禁用的代理队列表格），
    // 查看延时/流量改为点击 IP（见下方 mousedown 处理，点击后 pin，可点 ✕/外部关闭）
    const lockProxy = document.getElementById('sb-lock-proxy');
    lockProxy.addEventListener('mouseenter', () => {
      cancelShowProxyTooltip();
    });
    lockProxy.addEventListener('mouseleave', () => {
      cancelShowProxyTooltip();
      scheduleHideProxyTooltip();
    });

    // 锁定卡片 IP 文本选择保护（与表格 selectingText 同机制）：
    // 按下即冻结锁定卡片 DOM 更新（textContent/innerHTML 重建会打断选区），
    // 拖选完成有选区则保持冻结直到选区消失，方便复制 IP
    lockProxy.addEventListener('mousedown', (e) => {
      lockSelectingText = true;
      const onUp = (ev) => {
        document.removeEventListener('mouseup', onUp, true);
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) {
          // 有文本选区：保持冻结直到选区消失（与表格 selectingText 一致）
          if (lockSelectingTextChangeHandler) document.removeEventListener('selectionchange', lockSelectingTextChangeHandler);
          lockSelectingTextChangeHandler = () => {
            if (!window.getSelection().toString()) {
              lockSelectingText = false;
              document.removeEventListener('selectionchange', lockSelectingTextChangeHandler);
              lockSelectingTextChangeHandler = null;
            }
          };
          document.addEventListener('selectionchange', lockSelectingTextChangeHandler);
        } else {
          lockSelectingText = false;
          // 无选区 → 视为点击：显示统计浮窗并 pin（查看锁定代理延时/流量，不自动弹出遮住表格）
          const proxy = lockProxy.textContent;
          if (proxy && !window.getSelection().toString()) {
            showProxyTooltip(utils.bindOf(proxy), ev, 'lock', true);
            tooltipPinned = true;
          }
        }
      };
      document.addEventListener('mouseup', onUp, true);
    });

    // 右键表格行：弹出上下文菜单（移出/拉黑/测速）；右键目标行未选中时先单选它
    wrap.addEventListener('contextmenu', (e) => {
      // 有文本选区时让浏览器处理原生右键菜单（复制等），不接管
      if (window.getSelection().toString()) return;
      const tr = e.target.closest('tbody tr');
      if (!tr) return;
      const proxy = tr.dataset.proxy;
      if (!proxy) return;
      e.preventDefault();
      if (!selection.has(proxy)) selection.selectOne(proxy);
      showContextMenu(proxy, e);
    });

    // 行选择控制器（Windows 资源管理器式框选/多选）
    // 排除 code（保留点击 pin / hover tooltip）与按钮，行选中仅限非 code 区域
    selection = window.createRowSelection({
      container: wrap,
      scrollEl: document.getElementById('main-content'),
      getRows: () => currentStandby,
      proxyOf: (r) => r.proxy,
      estimateIndexAtY: (clientY) => {
        if (!currentStandby.length) return -1;
        const tbody = wrap.querySelector('tbody');
        if (!tbody || !tbody.children.length) return -1;
        const rows = tbody.children;
        const first = rows[0].getBoundingClientRect();
        const last = rows[rows.length - 1].getBoundingClientRect();
        if (clientY < first.top) return 0;                 // 表头上方 → 首行
        if (clientY > last.bottom) return rows.length - 1; // 表底下方 → 末行
        for (let i = 0; i < rows.length; i++) {
          const r = rows[i].getBoundingClientRect();
          if (clientY >= r.top && clientY <= r.bottom) return i;
        }
        return 0;
      },
      isExcludedTarget: (e) => !!e.target.closest('code, button'),
      onChanged: applyHighlight,
      isActive: () => window.appState.currentPage === 'standby',
      onDragStart: hideProxyTooltip,  // 拖拽框出现时立刻关闭悬浮面板
    });

    // 主区域滚动时关闭悬浮面板（tooltip 为 fixed 定位，不跟随滚动；pin 状态保留）
    document.getElementById('main-content').addEventListener('scroll', () => {
      if (hoverTooltip && !tooltipPinned) hideProxyTooltip();
    }, { passive: true });

    // 点击代理 IP → pin tooltip；按下后移动（拖选文本）→ 立即隐藏悬浮面板
    wrap.addEventListener('mousedown', (e) => {
      const code = e.target.closest('code');
      if (!code) return;
      selectingText = true;  // 按下即冻结重绘（拖选文本/点击，松开后恢复），避免 2s 重绘清空选区
      const startX = e.clientX, startY = e.clientY;
      let moved = false;
      const cleanup = () => {
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
      };
      const onMove = (ev) => {
        // 按下状态移动超过阈值（开始拖选文本）→ 立刻隐藏悬浮面板
        if (Math.hypot(ev.clientX - startX, ev.clientY - startY) > 4) {
          moved = true;
          document.removeEventListener('mousemove', onMove, true);
          hideProxyTooltip();
        }
      };
      const onUp = () => {
        cleanup();
        // 松开后若存在文本选区（拖选了文本），保持冻结直到选区消失（方便复制）
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) {
          selectingText = true;
          if (selectingTextChangeHandler) document.removeEventListener('selectionchange', selectingTextChangeHandler);
          selectingTextChangeHandler = () => {
            if (!window.getSelection().toString()) {
              selectingText = false;
              document.removeEventListener('selectionchange', selectingTextChangeHandler);
              selectingTextChangeHandler = null;
            }
          };
          document.addEventListener('selectionchange', selectingTextChangeHandler);
        } else {
          selectingText = false;
        }
        if (moved) return;
        // 无移动 → 视为点击 → pin tooltip
        const bind = utils.bindOf(code.textContent);
        if (hoverBind === bind) tooltipPinned = true;
      };
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });

    // pin 状态下在 tooltip 外部按下鼠标 → 立即销毁 tooltip（解除 pin）
    // 排除表格内 code 按下：pin 由上方 mousedown 处理，不在此销毁
    document.addEventListener('mousedown', (e) => {
      if (!tooltipPinned || !hoverTooltip) return;
      if (e.target.closest('#sb-table-wrap code')) return;
      if (!hoverTooltip.contains(e.target)) hideProxyTooltip();
    }, true);
  }

  function renderTable(standby) {
    const wrap = document.getElementById('sb-table-wrap');
    if (!standby.length) {
      wrap.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
          <div>代理服务队列为空</div>
          <div class="text-xs mt-1">等待 serve 主循环自动补入</div>
        </div>
      `;
      return;
    }
    wrap.innerHTML = `
      <table>
        <thead><tr><th>#</th><th>代理</th><th>延迟</th><th>连续失败</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          ${standby.map((p, i) => {
            const bind = utils.bindOf(p.proxy);
            const purityTags = renderPurityTags(purityCache.get(bind) || p.purity);
            const purityBtn = purityBtnHtml(p.proxy);
            const pinBtn = p.pinned
              ? `<button class="btn btn-ghost btn-sm" data-action="unpin" data-proxy="${utils.esc(p.proxy)}">解固</button>`
              : `<button class="btn btn-warning btn-sm" data-action="pin" data-proxy="${utils.esc(p.proxy)}">固定</button>`;
            const lockBtn = `<button class="btn btn-danger btn-sm" data-action="lock" data-proxy="${utils.esc(p.proxy)}">锁定</button>`;
            return `
              <tr data-idx="${i}" data-proxy="${utils.esc(p.proxy)}">
                <td class="text-theme-muted w-12">${i + 1}</td>
                <td><code>${utils.esc(p.proxy)}</code>${purityTags}</td>
                <td>${utils.fmtLatency(p.latency)}</td>
                <td>${p.consecutive_fails ?? 0}</td>
                <td class="text-right whitespace-nowrap">${purityBtn} ${pinBtn} ${lockBtn}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
    currentStandby = standby;  // 缓存供 selection 读取
    wrap.querySelectorAll('button[data-action]').forEach((btn) => {
      btn.onclick = () => {
        const action = btn.dataset.action;
        const proxy = btn.dataset.proxy;
        if (action === 'purity') doPurity(proxy);
        else if (action === 'pin') doPin(proxy);
        else if (action === 'unpin') doUnpin(proxy);
        else if (action === 'lock') doLock(proxy);
      };
    });
    if (selection) selection.refresh();  // 重渲染后重放高亮
    // 表格重绘后：若 hover 的代理来自表格且已不在队列，销毁 tooltip
    // 锁定卡片的代理在 LOCKED_GROUP 不在 standby 队列，跳过检查
    // pin 状态（测速中/手动 pin）不强制销毁：tooltip 内容自洽，代理离队不影响查看
    if (hoverBind && hoverSource === 'table' && !tooltipPinned) {
      const found = standby.some((p) => utils.bindOf(p.proxy) === hoverBind);
      if (!found) {
        hideProxyTooltip();
      }
    }
  }

  async function doPin(proxy) {
    try {
      await window.api.pin(proxy);
      showToast('success', '已固定', '代理已移到队列最前');
    } catch (e) {
      showToast('error', '固定失败', e.message);
    }
  }

  async function doUnpin(proxy) {
    try {
      await window.api.unpin(proxy);
      showToast('success', '已解固', '代理回到延迟排序');
    } catch (e) {
      showToast('error', '解固失败', e.message);
    }
  }

  async function doLock(proxy) {
    try {
      await window.api.lock(proxy);
      showToast('success', '已锁定', `只使用 ${proxy}`);
    } catch (e) {
      showToast('error', '锁定失败', e.message);
    }
  }

  async function doUnlock() {
    try {
      await window.api.unlock();
      showToast('success', '已解锁', '恢复代理服务队列');
    } catch (e) {
      showToast('error', '解锁失败', e.message);
    }
  }

  // ---------------- AbuseIPDB 纯洁度检查 ----------------
  async function doPurity(proxy) {
    const bind = utils.bindOf(proxy);
    purityCache.set(bind, { status: 'checking' });
    renderTable(currentStandby);  // 立即更新按钮为"检查中"
    try {
      const r = await window.api.purity(bind);
      purityCache.set(bind, { status: 'done', ...r });
    } catch (e) {
      purityCache.set(bind, { status: 'error', error: e.message });
      showToast('error', '纯洁度检查失败', e.message);
    }
    renderTable(currentStandby);  // 重渲染显示 tag
    // 锁定卡片的 tag 也同步更新
    const lockTagsEl = document.getElementById('sb-lock-purity-tags');
    if (lockTagsEl && document.getElementById('sb-lock-card').style.display !== 'none') {
      const lockProxy = document.getElementById('sb-lock-proxy').textContent;
      if (lockProxy && utils.bindOf(lockProxy) === bind) {
        lockTagsEl.innerHTML = renderPurityTags(purityCache.get(bind));
      }
    }
  }

  // ---------------- 右键上下文菜单：移出（子菜单选目标组）/ 拉黑 / 测速 ----------------
  function showContextMenu(proxy, mouseEvent) {
    hideContextMenu();
    const bind = utils.bindOf(proxy);

    // 移出子菜单：列出所有组（排除 standby 自身和 blacklist——blacklist 用"拉黑"项）
    const groupItems = Object.entries(standbyGroups)
      .filter(([, g]) => g.kind !== 'standby' && g.kind !== 'blacklist')
      .map(([name, g]) => `
        <div class="context-menu-item" data-action="move" data-group="${utils.esc(name)}">
          <span>${utils.esc(g.label)}</span>
          <span class="text-theme-muted text-xs">${g.count}</span>
        </div>
      `).join('');

    const selCount = selection ? selection.count() : 0;
    // 多选时不展示测速（测速仅单选可用）
    const speedItem = selCount > 1
      ? ''
      : '<div class="context-menu-item" data-action="speedtest"><span>测速</span></div>';

    contextMenu = document.createElement('div');
    contextMenu.className = 'context-menu';
    contextMenu.innerHTML = `
      <div class="context-menu-item context-menu-has-submenu">
        <span>移出</span>
        <svg class="context-menu-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        <div class="context-submenu">${groupItems}</div>
      </div>
      <div class="context-menu-item" data-action="blacklist"><span>拉黑</span></div>
      ${speedItem}
    `;
    document.body.appendChild(contextMenu);
    positionContextMenu(mouseEvent);

    contextMenu.addEventListener('click', (e) => {
      const item = e.target.closest('.context-menu-item');
      if (!item || !item.dataset.action) return;
      const action = item.dataset.action;
      if (action === 'move') {
        doMove(proxy, item.dataset.group);
        hideContextMenu();
      } else if (action === 'blacklist') {
        doBlacklist(proxy);
        hideContextMenu();
      } else if (action === 'speedtest') {
        hideContextMenu();
        showProxyTooltip(bind, mouseEvent, 'table', true);
        runSpeedTest(bind);
      }
    });

    // 点击菜单外部 / 滚动 / 窗口缩放 → 关闭菜单
    document.addEventListener('click', hideContextMenuOnOutside, true);
    document.addEventListener('scroll', hideContextMenu, true);
    window.addEventListener('resize', hideContextMenu);
  }

  function hideContextMenuOnOutside(e) {
    if (contextMenu && !contextMenu.contains(e.target)) hideContextMenu();
  }

  function hideContextMenu() {
    if (contextMenu) { contextMenu.remove(); contextMenu = null; }
    document.removeEventListener('click', hideContextMenuOnOutside, true);
    document.removeEventListener('scroll', hideContextMenu, true);
    window.removeEventListener('resize', hideContextMenu);
  }

  function positionContextMenu(e) {
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

  async function doMove(proxy, targetGroup) {
    const proxies = selectedProxies() || [proxy];
    try {
      const d = await window.api.moveProxy(proxies, targetGroup);
      const label = standbyGroups[targetGroup]?.label || targetGroup;
      showToast('success', '已移出', `已将 ${d.count} 个代理移到 ${label}`);
      selection && selection.clear();
    } catch (e) {
      showToast('error', '移出失败', e.message);
    }
  }

  async function doBlacklist(proxy) {
    const proxies = selectedProxies() || [proxy];
    try {
      const d = await window.api.blacklist(proxies);
      showToast('success', '已拉黑', `已将 ${d.count} 个代理加入黑名单`);
      selection && selection.clear();
    } catch (e) {
      showToast('error', '拉黑失败', e.message);
    }
  }

  // ---------------- 代理 IP hover tooltip：展示该 IP 的延时+流量图表 ----------------
  // 悬停 300ms 才显示，避免鼠标划过时误触发
  function scheduleShowProxyTooltip(bind, mouseEvent, source) {
    cancelShowProxyTooltip();
    hoverShowTimer = setTimeout(() => {
      hoverShowTimer = null;
      showProxyTooltip(bind, mouseEvent, source);
    }, 300);
  }

  function cancelShowProxyTooltip() {
    if (hoverShowTimer) { clearTimeout(hoverShowTimer); hoverShowTimer = null; }
  }

  function showProxyTooltip(bind, mouseEvent, source, force) {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }  // 取消延迟销毁
    if (hoverBind === bind) return;
    // 单实例：已有 pin 的 tooltip 时，不允许悬浮切换（force=true 时强制切换，如右键菜单"测速"）
    if (tooltipPinned && !force) return;
    hideProxyTooltip();
    hoverBind = bind;
    hoverSource = source;

    hoverTooltip = document.createElement('div');
    hoverTooltip.className = 'proxy-tooltip';
    hoverTooltip.innerHTML = `
      <div class="proxy-tooltip-header">
        <code>${utils.esc(bind)}</code>
        <span class="proxy-tooltip-stats" id="proxy-tooltip-stats">加载中...</span>
        <button class="proxy-tooltip-close" id="proxy-tooltip-close" title="关闭">✕</button>
      </div>
      <div class="proxy-tooltip-charts"></div>
      <div class="proxy-tooltip-speedtest">
        <button class="btn btn-ghost btn-sm w-full" id="proxy-speedtest-btn">测速 (CF 10s)</button>
        <div class="proxy-tooltip-speedtest-result" id="proxy-speedtest-result"></div>
      </div>
    `;
    document.body.appendChild(hoverTooltip);

    // 鼠标进入 tooltip 时取消延迟销毁，离开时延迟销毁（允许用户与图表交互）
    hoverTooltip.addEventListener('mouseenter', () => {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    });
    hoverTooltip.addEventListener('mouseleave', () => {
      scheduleHideProxyTooltip();
    });

    // 关闭按钮：随时可关闭浮窗（尤其 pin 状态/测速中，避免一直遮住表格）
    hoverTooltip.querySelector('#proxy-tooltip-close').addEventListener('click', () => {
      hideProxyTooltip();
    });

    const chartsContainer = hoverTooltip.querySelector('.proxy-tooltip-charts');
    hoverCharts = window.charts.createProxyCharts(chartsContainer);
    positionTooltip(hoverTooltip, mouseEvent);
    refreshProxyTooltip(bind);
    hoverTimer = setInterval(() => refreshProxyTooltip(bind), 2000);

    // 测速按钮：点击后调 pspeed 对该代理做 CF 下载+上传 10s 测速
    hoverTooltip.querySelector('#proxy-speedtest-btn').addEventListener('click', () => {
      runSpeedTest(bind);
    });

    // 恢复测速状态（tooltip 重建时保留之前的测速进度/结果）
    if (speedTestState && speedTestState.bind === bind) {
      renderSpeedTestState();
    }
  }

  async function refreshProxyTooltip(bind) {
    try {
      const data = await window.api.proxyHistory(bind);
      if (hoverBind !== bind) return;
      hoverCharts.render(data.latency_history, data.traffic_history);
      const lh = data.latency_history || [];
      const th = data.traffic_history || [];
      const statsEl = hoverTooltip && hoverTooltip.querySelector('#proxy-tooltip-stats');
      if (statsEl) {
        const lastLat = lh.length ? lh[lh.length - 1].latency : null;
        const totalDown = th.length ? th[th.length - 1].total_download : 0;
        const totalUp = th.length ? th[th.length - 1].total_upload : 0;
        statsEl.textContent = `延迟 ${lastLat != null ? lastLat.toFixed(3) + 's' : '-'} · ↑${utils.fmtBytes(totalUp)} ↓${utils.fmtBytes(totalDown)}`;
      }
    } catch (_) { /* 忽略 */ }
  }

  function positionTooltip(el, e) {
    el.style.visibility = 'hidden';
    requestAnimationFrame(() => {
      const rect = el.getBoundingClientRect();
      const maxX = window.innerWidth - rect.width - 8;
      const maxY = window.innerHeight - rect.height - 8;
      // 优先显示在鼠标右侧；右侧放不下则翻转到左侧，避免越出视口挤压遮挡
      let x = e.clientX + 14;
      if (x + rect.width > window.innerWidth - 8) x = e.clientX - 14 - rect.width;
      const y = e.clientY + 14;
      el.style.left = Math.min(Math.max(x, 8), maxX) + 'px';
      el.style.top = Math.min(Math.max(y, 8), maxY) + 'px';
      el.style.visibility = '';
    });
  }

  function hideProxyTooltip() {
    cancelShowProxyTooltip();
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (hoverTimer) { clearInterval(hoverTimer); hoverTimer = null; }
    if (hoverCharts) { hoverCharts.destroy(); hoverCharts = null; }
    if (hoverTooltip) { hoverTooltip.remove(); hoverTooltip = null; }
    hoverBind = null;
    hoverSource = null;
    tooltipPinned = false;  // 解除 pin，下次显示时恢复正常 mouseleave 行为
  }

  function scheduleHideProxyTooltip() {
    // 测速中/完成后 tooltip pin：不自动消失，仅点击外部才消失
    if (tooltipPinned) return;
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { hideProxyTooltip(); hideTimer = null; }, 100);
  }

  // 测速：调后端 /api/proxy/speedtest，用 pspeed 对该代理做 CF 下载+上传 10s 测速
  // speedTestState 保留测速状态，tooltip 隐藏再重新显示时恢复（测速不中断、结果不丢失）
  function renderSpeedTestState() {
    const btn = document.getElementById('proxy-speedtest-btn');
    const resultEl = document.getElementById('proxy-speedtest-result');
    if (!btn || !resultEl || !speedTestState) return;
    if (speedTestState.status === 'running') {
      btn.disabled = true;
      btn.textContent = '测速中...';
      resultEl.innerHTML = '<span class="text-theme-muted">正在测速（最多 10s）...</span>';
    } else if (speedTestState.status === 'done') {
      btn.disabled = false;
      btn.textContent = '测速 (CF 10s)';
      if (speedTestState.error) {
        resultEl.innerHTML = `<span class="text-theme-warning">测速失败: ${utils.esc(speedTestState.error)}</span>`;
      } else {
        const r = speedTestState.result;
        const dl = r.download || {};
        const ul = r.upload || {};
        const dlText = dl.ok ? `${dl.best_mbps.toFixed(2)} Mbps` : `失败${dl.err ? ' (' + utils.esc(dl.err) + ')' : ''}`;
        const ulText = ul.ok ? `${ul.best_mbps.toFixed(2)} Mbps` : `失败${ul.err ? ' (' + utils.esc(ul.err) + ')' : ''}`;
        resultEl.innerHTML = `
          <div class="flex justify-between"><span class="text-theme-muted">下载</span><span class="font-medium">${dlText}</span></div>
          <div class="flex justify-between"><span class="text-theme-muted">上传</span><span class="font-medium">${ulText}</span></div>
          <div class="text-theme-muted text-xs mt-1">测速时间: ${speedTestState.time}</div>
        `;
      }
    }
  }

  async function runSpeedTest(bind) {
    speedTestState = { status: 'running', bind };
    tooltipPinned = true;  // 测速开始：pin tooltip，mouseleave 不自动消失
    renderSpeedTestState();
    try {
      const data = await window.api.speedTest(bind);
      const now = new Date();
      const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
      speedTestState = { status: 'done', bind, result: data, time };
    } catch (e) {
      const now = new Date();
      const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
      speedTestState = { status: 'done', bind, error: e.message, time };
    }
    // tooltip 仍在显示该代理时更新 UI；否则只更新 speedTestState，等下次显示时恢复
    if (hoverBind === bind) renderSpeedTestState();
  }

  function renderStats(standby, capacity, locked, history) {
    if (locked) {
      // 锁定时只显示锁定代理的延迟（从最新采样点；_sample 传锁定代理，反映其测速）
      const last = history && history.length ? history[history.length - 1] : null;
      const avg = last && last.avg != null ? last.avg.toFixed(3) : '-';
      const min = last && last.min != null ? last.min.toFixed(3) : '-';
      const max = last && last.max != null ? last.max.toFixed(3) : '-';
      document.getElementById('sb-stats').innerHTML = `
        <div class="flex justify-between"><span class="text-theme-muted">平均延迟</span><span class="font-medium">${avg}s</span></div>
        <div class="flex justify-between"><span class="text-theme-muted">最小延迟</span><span class="font-medium text-theme-success">${min}s</span></div>
        <div class="flex justify-between"><span class="text-theme-muted">最大延迟</span><span class="font-medium text-theme-warning">${max}s</span></div>
      `;
      return;
    }
    const latencies = standby.map((x) => x.latency).filter((v) => v != null);
    const avg = latencies.length ? (latencies.reduce((a, b) => a + b, 0) / latencies.length).toFixed(3) : '-';
    const min = latencies.length ? Math.min(...latencies).toFixed(3) : '-';
    const max = latencies.length ? Math.max(...latencies).toFixed(3) : '-';
    const healthy = standby.filter((x) => (x.consecutive_fails || 0) === 0).length;
    const rate = standby.length ? Math.round((healthy / standby.length) * 100) : 0;

    document.getElementById('sb-stats').innerHTML = `
      <div class="flex justify-between"><span class="text-theme-muted">容量</span><span class="font-medium">${standby.length} / ${capacity}</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">健康率</span><span class="font-medium text-theme-success">${rate}%</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">平均延迟</span><span class="font-medium">${avg}s</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">最小延迟</span><span class="font-medium text-theme-success">${min}s</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">最大延迟</span><span class="font-medium text-theme-warning">${max}s</span></div>
    `;
  }

  // 调度算法选项：值 → 中文说明
  const ALG_LABELS = { fa: '首个可用 (fa)', rr: '轮询 (rr)', rc: '随机 (rc)', lc: '最少连接 (lc)' };

  function renderSettings(settings) {
    // 轮询覆盖保护：焦点在编辑区时跳过重绘，避免 2s 轮询清空正在编辑的输入
    if (document.activeElement && document.activeElement.closest('#sb-settings')) return;

    const algOptions = Object.keys(ALG_LABELS)
      .map((k) => `<option value="${k}" ${k === settings.salgorithm ? 'selected' : ''}>${ALG_LABELS[k]}</option>`)
      .join('');

    document.getElementById('sb-settings').innerHTML = `
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">心跳间隔</span>
        <div class="flex items-center gap-1">
          <input type="number" class="input w-20 text-right" id="set-interval" min="1" step="1" value="${settings.interval}">
          <span class="text-theme-muted text-xs">s</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">并发</span>
        <div class="flex items-center gap-1">
          <input type="number" class="input w-20 text-right" id="set-concurrency" min="1" step="1" value="${settings.concurrency}">
          <span class="text-theme-muted text-xs">x</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">超时</span>
        <div class="flex items-center gap-1">
          <input type="number" class="input w-20 text-right" id="set-timeout" min="0.5" step="0.5" value="${settings.timeout}">
          <span class="text-theme-muted text-xs">s</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">调度算法</span>
        <select class="select w-40" id="set-salgorithm">${algOptions}</select>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">允许类型</span>
        <span class="font-medium text-theme-muted">${(settings.allowed_types || []).join(', ') || '-'}</span>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-theme-muted">探测 URL</span>
        <input type="text" class="input w-48" id="set-probe-url" value="${utils.esc(settings.probe_url)}">
      </div>
      <div class="flex justify-end pt-1">
        <button class="btn btn-primary btn-sm" id="set-save" disabled>保存</button>
      </div>
    `;

    const btn = document.getElementById('set-save');
    btn.onclick = saveSettings;
    settingsBaseline = {
      interval: settings.interval,
      concurrency: settings.concurrency,
      timeout: settings.timeout,
      salgorithm: settings.salgorithm,
      probe_url: settings.probe_url,
    };
    ['set-interval', 'set-concurrency', 'set-timeout', 'set-salgorithm', 'set-probe-url']
      .forEach((id) => document.getElementById(id).addEventListener('input', updateSaveButton));
  }

  // 检测调度参数是否有改动，控制保存按钮启停（无改动时禁用）
  function updateSaveButton() {
    const btn = document.getElementById('set-save');
    if (!btn || !settingsBaseline) return;
    const dirty = parseInt(document.getElementById('set-interval').value, 10) !== settingsBaseline.interval
      || parseInt(document.getElementById('set-concurrency').value, 10) !== settingsBaseline.concurrency
      || parseFloat(document.getElementById('set-timeout').value) !== settingsBaseline.timeout
      || document.getElementById('set-salgorithm').value !== settingsBaseline.salgorithm
      || document.getElementById('set-probe-url').value.trim() !== settingsBaseline.probe_url;
    btn.disabled = !dirty;
  }

  // 保存调度参数：客户端校验 → POST /api/settings → 即刻生效（serve 下一轮读取最新值）
  async function saveSettings() {
    const btn = document.getElementById('set-save');
    const interval = parseInt(document.getElementById('set-interval').value, 10);
    const concurrency = parseInt(document.getElementById('set-concurrency').value, 10);
    const timeout = parseFloat(document.getElementById('set-timeout').value);
    const salgorithm = document.getElementById('set-salgorithm').value;
    const probeUrl = document.getElementById('set-probe-url').value.trim();

    if (!Number.isFinite(interval) || interval < 1) {
      showToast('error', '参数错误', '心跳间隔必须为正整数');
      return;
    }
    if (!Number.isFinite(concurrency) || concurrency < 1) {
      showToast('error', '参数错误', '并发必须为正整数');
      return;
    }
    if (!Number.isFinite(timeout) || timeout <= 0) {
      showToast('error', '参数错误', '超时必须为正数');
      return;
    }
    if (!probeUrl) {
      showToast('error', '参数错误', '探测 URL 不能为空');
      return;
    }

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      const d = await window.api.settings({ interval, concurrency, timeout, salgorithm, probe_url: probeUrl });
      showToast('success', '已更新', '调度参数即刻生效');
      btn.blur();
      if (d.settings) renderSettings(d.settings);
    } catch (e) {
      showToast('error', '保存失败', e.message);
    } finally {
      btn.textContent = originalText;
      updateSaveButton();
    }
  }

  function renderPending(pending) {
    const card = document.getElementById('sb-pending-card');
    const el = document.getElementById('sb-pending');
    if (!pending || !pending.length) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';
    el.innerHTML = pending.map((p) => `<div class="py-1"><code>${utils.esc(p.split('://')[1] || p)}</code></div>`).join('');
  }

  // 保持旧顺序合并新数据：拖拽/选中时避免延时重排导致行跑位
  // 旧顺序中仍在新列表的代理保留位置（数据更新），被踢出的移除，新加入的追加末尾
  function mergeKeepOrder(oldList, newList) {
    const newMap = new Map(newList.map((p) => [p.proxy, p]));
    const result = [];
    const seen = new Set();
    for (const p of oldList) {
      const fresh = newMap.get(p.proxy);
      if (fresh) { result.push(fresh); seen.add(p.proxy); }
    }
    for (const p of newList) {
      if (!seen.has(p.proxy)) result.push(p);
    }
    return result;
  }

  function onStatus(status) {
    if (window.appState.currentPage !== 'standby') return;
    render();
    const standby = status.standby || [];
    const lockedProxy = status.locked_proxy || null;
    standbyGroups = status.groups || {};  // 缓存供右键菜单移出子菜单用

    // 锁定状态：显示锁定卡片 + 禁用队列卡片 + 隐藏调度参数卡片
    const lockCard = document.getElementById('sb-lock-card');
    const queueCard = document.getElementById('sb-queue-card');
    const settingsCard = document.getElementById('sb-settings-card');
    if (lockedProxy) {
      lockCard.style.display = 'block';
      // 锁定代理的纯洁度按钮 + tag
      const lockBind = utils.bindOf(lockedProxy);
      // 文本选择保护：有选区时不重建锁定卡片 DOM（textContent/innerHTML 重建会打断选区），
      // 选区消失后下一轮轮询恢复更新。锁定代理文本变化必经卡片隐藏→重新显示，此处跳过无信息损失。
      const lockTextSelecting = lockSelectingText
        || (window.getSelection() && window.getSelection().toString().length > 0);
      if (!lockTextSelecting) {
        // 仅文本变化时重写 textContent：2s 轮询重建文本节点会打断用户正在进行的 IP 文本选择
        const lockProxyEl = document.getElementById('sb-lock-proxy');
        if (lockProxyEl.textContent !== lockedProxy) {
          lockProxyEl.textContent = lockedProxy;
        }
        document.getElementById('sb-lock-purity-tags').innerHTML = renderPurityTags(purityCache.get(lockBind) || status.locked_purity);
      }
      document.getElementById('sb-unlock-btn').onclick = doUnlock;
      document.getElementById('sb-lock-purity-btn').onclick = () => doPurity(lockedProxy);
      queueCard.classList.add('opacity-50', 'pointer-events-none');
      settingsCard.style.display = 'none';
    } else {
      lockCard.style.display = 'none';
      queueCard.classList.remove('opacity-50', 'pointer-events-none');
      settingsCard.style.display = '';
    }

    // 拖选文本期间跳过表格重绘（DOM 重建会清空选区）；锁定卡片选区可能跨越到表格，一并冻结
    const anyTextSelection = window.getSelection() && window.getSelection().toString().length > 0;
    if (!selectingText && !lockSelectingText && !anyTextSelection) {
      // 拖拽中/有选中/鼠标悬浮某行时保持当前顺序（不按延时重排），避免行跑位/悬浮行变更
      const freezeOrder = (selection && (selection.isDragging() || selection.count() > 0)) || !!hoveredProxy;
      const effectiveStandby = freezeOrder ? mergeKeepOrder(currentStandby, standby) : standby;
      renderTable(effectiveStandby);
    }
    renderStats(standby, status.capacity || '-', !!lockedProxy, status.standby_history || []);
    if (!lockedProxy) renderSettings(status.settings || {});
    renderPending(status.pending_retries || []);
  }

  window.pageHandlers.standby = { render, onStatus };
})();
