/**
 * 代理源页面：源列表、状态、暂停/恢复、手动拉取。
 */
(function () {
  'use strict';

  const page = document.getElementById('page-sources');
  let initialized = false;

  function render() {
    if (initialized) return;
    page.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">代理源</div>
            <div class="card-subtitle">建议及时更新</div>
          </div>
          <button class="btn btn-primary btn-sm" id="src-fetch">拉取代理</button>
        </div>
        <div class="table-wrap border-0 rounded-t-none" id="src-table-wrap">
          <div class="empty-state">暂无数据</div>
        </div>
      </div>
    `;

    document.getElementById('src-fetch').onclick = async () => {
      setLoading('src-fetch', true);
      try {
        const d = await window.api.fetch();
        showToast('success', '拉取完成', `可用代理 ${d.available}`);
      } catch (e) {
        showToast('error', '拉取失败', e.message);
      }
      setLoading('src-fetch', false);
    };

    page.addEventListener('click', handleAction);
    initialized = true;
  }

  function handleAction(e) {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const source = btn.dataset.source;
    if (action === 'pause') doPause(source);
    else if (action === 'resume') doResume(source);
  }

  async function doPause(source) {
    try {
      await window.api.pauseSource(source);
      showToast('success', '已暂停', source);
    } catch (e) {
      showToast('error', '暂停失败', e.message);
    }
  }

  async function doResume(source) {
    try {
      await window.api.resumeSource(source);
      showToast('success', '已恢复', source);
    } catch (e) {
      showToast('error', '恢复失败', e.message);
    }
  }

  function renderTable(sources) {
    const wrap = document.getElementById('src-table-wrap');
    if (!sources.length) {
      wrap.innerHTML = '<div class="empty-state">未配置代理源</div>';
      return;
    }
    wrap.innerHTML = `
      <table>
        <thead>
          <tr><th>源</th><th>状态</th><th>暂停剩余</th><th class="text-right">操作</th></tr>
        </thead>
        <tbody>
          ${sources.map((s) => {
            let statusBadge;
            if (!s.enabled) statusBadge = '<span class="badge badge-muted">禁用</span>';
            else if (s.paused) statusBadge = '<span class="badge badge-warning">已暂停</span>';
            else statusBadge = '<span class="badge badge-success">正常</span>';
            const action = s.paused
              ? `<button class="btn btn-ghost btn-sm" data-action="resume" data-source="${utils.esc(s.name)}">恢复</button>`
              : `<button class="btn btn-ghost btn-sm" data-action="pause" data-source="${utils.esc(s.name)}">暂停</button>`;
            return `
              <tr class="${s.paused ? 'bg-theme-highlight' : ''}">
                <td><code>${utils.esc(s.short_name)}</code><div class="text-xs text-theme-muted mt-0.5">${utils.esc(s.name)}</div></td>
                <td>${statusBadge}</td>
                <td class="text-theme-muted">${s.pause_remaining != null ? utils.fmtDuration(s.pause_remaining) : '-'}</td>
                <td class="text-right">${s.enabled ? action : '-'}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  }

  function onStatus(status) {
    if (window.appState.currentPage !== 'sources') return;
    render();
    renderTable(status.sources || []);
  }

  function setLoading(id, loading) {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = loading;
    btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
    btn.textContent = loading ? '处理中…' : btn.dataset.originalText;
  }

  window.pageHandlers.sources = { render, onStatus };
})();
