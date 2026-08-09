/**
 * 应用核心：路由、主题、自动刷新、连接状态、Toast、页面调度。
 */
(function () {
  'use strict';

  const REFRESH_INTERVAL = 2000;

  window.appState = {
    status: null,
    lastStatusTime: 0,
    paused: false,
    refreshTimer: null,
    currentPage: 'dashboard',
    theme: 'light',
    isFirstLoad: true,
  };

  window.pageHandlers = window.pageHandlers || {};

  window.utils = {
    esc(text) {
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },
    fmtTtl(ttl) {
      return ttl == null ? '永不过期' : `${ttl}s`;
    },
    fmtDuration(seconds) {
      if (seconds == null) return '-';
      if (seconds < 60) return `${seconds}s`;
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      return s ? `${m}m ${s}s` : `${m}m`;
    },
    fmtLatency(value) {
      return value == null ? '-' : `${value.toFixed(3)}s`;
    },
    bindOf(proxy) {
      return proxy.includes('://') ? proxy.split('://')[1] : proxy;
    },
    fmtBytes(n) {
      if (n == null) return '-';
      if (n < 1024) return `${n}B`;
      if (n < 1048576) return `${(n / 1024).toFixed(1)}KB`;
      if (n < 1073741824) return `${(n / 1048576).toFixed(1)}MB`;
      return `${(n / 1073741824).toFixed(2)}GB`;
    },
    nowStr() {
      const d = new Date();
      return d.toLocaleTimeString('zh-CN', { hour12: false });
    },
  };

  // ---------------- Toast ----------------
  window.showToast = function (type, title, message, duration) {
    duration = duration || (type === 'error' ? 5000 : 3000);
    const container = document.getElementById('toast-container');
    const icons = {
      success: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>',
      error: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>',
      warning: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>',
      info: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>',
    };

    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.innerHTML = `
      ${icons[type] || icons.info}
      <div class="toast-content">
        <div class="toast-title">${utils.esc(title)}</div>
        ${message ? `<div class="toast-message">${utils.esc(message)}</div>` : ''}
      </div>
      <button class="toast-close" aria-label="关闭">✕</button>
    `;
    el.querySelector('.toast-close').onclick = () => removeToast(el);
    container.appendChild(el);

    const timeout = setTimeout(() => removeToast(el), duration);
    el.dataset.timeout = timeout;
  };

  function removeToast(el) {
    if (!el.parentNode) return;
    clearTimeout(Number(el.dataset.timeout));
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  }

  // ---------------- 连接状态 ----------------
  function setConnection(state) {
    const dot = document.getElementById('conn-dot');
    const text = document.getElementById('conn-text');
    dot.className = 'conn-dot ' + state;
    if (state === 'online') {
      text.textContent = '已连接';
      text.className = 'text-theme-success';
    } else {
      text.textContent = '已断开';
      text.className = 'text-theme-danger';
    }
  }

  // ---------------- 主题切换 ----------------
  function initTheme() {
    const saved = localStorage.getItem('proxy-pool-theme');
    // 默认浅色，只有用户手动切换过才沿用保存值
    applyTheme(saved === 'dark' ? 'dark' : 'light');
  }

  function applyTheme(theme) {
    window.appState.theme = theme;
    const html = document.documentElement;
    if (theme === 'dark') {
      html.classList.add('dark');
      html.setAttribute('data-theme', 'dark');
    } else {
      html.classList.remove('dark');
      html.setAttribute('data-theme', 'light');
    }
    document.getElementById('icon-sun').classList.toggle('hidden', theme === 'dark');
    document.getElementById('icon-moon').classList.toggle('hidden', theme !== 'dark');
    localStorage.setItem('proxy-pool-theme', theme);
    if (Chart && Chart.defaults) {
      Chart.defaults.color = getComputedStyle(document.body).getPropertyValue('--text-secondary').trim();
      Chart.defaults.borderColor = getComputedStyle(document.body).getPropertyValue('--border-color').trim();
    }
  }

  function toggleTheme() {
    applyTheme(window.appState.theme === 'dark' ? 'light' : 'dark');
  }

  // ---------------- 路由 ----------------
  const pageTitles = {
    dashboard: '仪表盘',
    groups: '代理组',
    standby: '代理服务队列',
    sources: '代理源',
  };

  function switchPage(page) {
    if (!pageHandlers[page]) page = 'dashboard';
    window.appState.currentPage = page;

    document.querySelectorAll('.nav-item').forEach((a) => {
      a.classList.toggle('active', a.dataset.page === page);
    });

    document.querySelectorAll('.page-section').forEach((sec) => {
      sec.classList.add('hidden');
    });
    document.getElementById('page-' + page).classList.remove('hidden');
    document.getElementById('page-title').textContent = pageTitles[page];

    if (pageHandlers[page].render) {
      pageHandlers[page].render();
    }
    if (window.appState.status && pageHandlers[page].onStatus) {
      pageHandlers[page].onStatus(window.appState.status);
    }

    // 移动端自动收起侧边栏
    closeSidebar();
  }

  function onHashChange() {
    const hash = window.location.hash.replace('#', '') || 'dashboard';
    switchPage(hash);
  }

  // ---------------- 自动刷新 ----------------
  async function tick() {
    if (window.appState.paused) return;
    try {
      const data = await window.api.status();
      window.appState.status = data;
      window.appState.lastStatusTime = Date.now();
      setConnection('online');

      const page = window.appState.currentPage;
      if (pageHandlers[page] && pageHandlers[page].onStatus) {
        pageHandlers[page].onStatus(data);
      }
      window.appState.isFirstLoad = false;
    } catch (err) {
      console.error('status refresh failed', err);
      setConnection('offline');
      if (window.appState.isFirstLoad) {
        window.appState.isFirstLoad = false;
        showToast('error', '连接失败', err.message);
      }
    }
  }

  function startRefresh() {
    if (window.appState.refreshTimer) return;
    window.appState.refreshTimer = setInterval(tick, REFRESH_INTERVAL);
    tick();
  }

  function togglePause() {
    window.appState.paused = !window.appState.paused;
    const paused = window.appState.paused;
    document.getElementById('icon-pause').classList.toggle('hidden', paused);
    document.getElementById('icon-play').classList.toggle('hidden', !paused);
    document.getElementById('text-pause').textContent = paused ? '继续' : '暂停';
    document.getElementById('btn-pause').classList.toggle('btn-ghost', !paused);
    document.getElementById('btn-pause').classList.toggle('btn-primary', paused);
    if (!paused) tick();
  }

  // ---------------- 侧边栏 ----------------
  function openSidebar() {
    document.getElementById('sidebar').classList.add('open');
    document.getElementById('sidebar-overlay').classList.add('open');
  }

  function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('open');
  }

  // ---------------- 初始化 ----------------
  function init() {
    initTheme();

    document.getElementById('btn-theme').onclick = toggleTheme;
    document.getElementById('btn-pause').onclick = togglePause;
    document.getElementById('menu-toggle').onclick = openSidebar;
    document.getElementById('sidebar-overlay').onclick = closeSidebar;

    document.querySelectorAll('.nav-item').forEach((a) => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        const page = a.dataset.page;
        window.location.hash = page;
      });
    });

    window.addEventListener('hashchange', onHashChange);
    onHashChange();
    startRefresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
