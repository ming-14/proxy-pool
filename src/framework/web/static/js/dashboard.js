/**
 * 仪表盘页面：KPI 卡片、图表趋势、流量监控。
 */
(function () {
  'use strict';

  window.pageHandlers = window.pageHandlers || {};

  const page = document.getElementById('page-dashboard');
  let initialized = false;

  function renderSkeleton() {
    page.innerHTML = `
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="skeleton skeleton-card"></div>
        <div class="skeleton skeleton-card"></div>
        <div class="skeleton skeleton-card"></div>
        <div class="skeleton skeleton-card"></div>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div class="card p-4"><div class="skeleton skeleton-title"></div><div class="h-56 skeleton"></div></div>
        <div class="card p-4"><div class="skeleton skeleton-title"></div><div class="h-56 skeleton"></div></div>
      </div>
    `;
  }

  function render() {
    if (initialized) return;
    renderSkeleton();
  }

  function kpiCard(label, value, sub, colorClass) {
    return `
      <div class="card card-body card-hover">
        <div class="kpi-label">${utils.esc(label)}</div>
        <div class="kpi-value ${colorClass || ''}">${utils.esc(value)}</div>
        ${sub ? `<div class="kpi-delta text-theme-muted">${sub}</div>` : ''}
      </div>
    `;
  }

  function renderContent(status) {
    if (!initialized) {
      // IP 分布地图区块：geoip.enabled=false 时不渲染（无 /api/geo/map 路由与坐标数据）
      const geoCard = status.geo_enabled ? `
        <div class="card p-4">
          <div class="flex items-center justify-between mb-4 flex-wrap gap-2">
            <div class="card-title">IP 分布地图</div>
            <div class="text-xs text-theme-muted" id="dash-geo-stats"></div>
          </div>
          <div id="dash-geo-map" style="height: 480px"></div>
        </div>` : '';
      page.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4" id="dash-kpis"></div>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <div class="card p-4" id="dash-latency-card">
            <div class="card-title mb-4">代理服务队列延迟趋势</div>
            <div class="h-56" id="dash-latency-chart"></div>
          </div>
          <div class="card p-4" id="dash-health-card">
            <div class="card-title mb-4">代理服务队列健康率趋势</div>
            <div class="h-56" id="dash-health-chart"></div>
          </div>
        </div>
        <div class="card p-4">
          <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div class="lg:col-span-2">
              <div class="card-title mb-4">数据流量</div>
              <div class="h-56" id="dash-traffic-chart"></div>
            </div>
            <div class="space-y-3 text-sm" id="dash-traffic-kpi"></div>
          </div>
        </div>
        ${geoCard}
        <div id="dash-pending"></div>
      `;
      window.charts.initLatency('dash-latency-chart');
      window.charts.initHealth('dash-health-chart');
      window.charts.initTraffic('dash-traffic-chart');
      initialized = true;
    }

    const groups = status.groups || {};
    const groupCount = Object.keys(groups).length;
    const standby = status.standby || [];
    const pending = status.pending_retries || [];
    const lockedProxy = status.locked_proxy || null;

    // 按 kind 查找组（不硬编码组名，由 API 下发 kind=label 配对）
    const findByKind = (kind) => {
      for (const name of Object.keys(groups)) {
        if (groups[name].kind === kind) return groups[name];
      }
      return null;
    };
    const trustedGroup = findByKind('trusted');
    const blacklistGroup = findByKind('blacklist');

    document.getElementById('dash-kpis').innerHTML = [
      kpiCard('可用代理', status.available_count ?? '-', `${groupCount} 个分组`, 'text-theme-accent'),
      kpiCard('代理服务队列',
        lockedProxy ? '固定代理' : `${standby.length} / ${status.capacity ?? '-'}`,
        lockedProxy ? utils.esc(lockedProxy) : 'pproxy 上游',
        lockedProxy ? 'text-theme-warning' : 'text-theme-success'),
      kpiCard(trustedGroup?.label ?? '信任池', trustedGroup?.count ?? '-', '请求成功晋级', 'text-theme-warning'),
      kpiCard(blacklistGroup?.label ?? '黑名单', blacklistGroup?.count ?? '-', '连续失败淘汰', 'text-theme-danger'),
    ].join('');

    // 锁定时隐藏健康率趋势卡片（健康率/队列长度无意义），延迟趋势卡片占满全宽
    document.getElementById('dash-health-card').style.display = lockedProxy ? 'none' : '';
    const latencyCard = document.getElementById('dash-latency-card');
    if (latencyCard) {
      latencyCard.style.gridColumn = lockedProxy ? '1 / -1' : '';
    }

    const pendingEl = document.getElementById('dash-pending');
    if (pending.length) {
      pendingEl.innerHTML = `
        <div class="card card-body">
          <div class="flex items-center gap-2 text-sm text-theme-secondary">
            <span class="badge badge-warning">待补测</span>
            <span>${pending.map((p) => utils.esc(p.split('://')[1] || p)).join(', ')}</span>
          </div>
        </div>
      `;
    } else if (pendingEl) {
      pendingEl.innerHTML = '';
    }

    window.charts.renderLatency(status.standby_history || []);
    window.charts.renderHealth(status.standby_history || []);
    window.charts.renderTraffic(status.traffic_history || [], status.standby_history || []);
    renderTrafficKpi(status);

    // IP 分布地图（内嵌仪表盘底部，内部 60s 节流，不随 2s 心跳刷）；geo 禁用时跳过
    if (status.geo_enabled && window.geoMap) {
      window.geoMap.refresh();
    }
  }

  // 字节自适应格式化：B / KB / MB / GB
  function fmtBytes(n) {
    if (n == null) return '-';
    if (n < 1024) return Math.round(n) + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  // 流量卡片旁 KPI：实时上下行速率、累计流量、当前平均延迟
  function renderTrafficKpi(status) {
    const el = document.getElementById('dash-traffic-kpi');
    if (!el) return;
    const th = status.traffic_history || [];
    let upRate = 0, downRate = 0;
    if (th.length >= 2) {
      const a = th[th.length - 2], b = th[th.length - 1];
      const dt = b.ts - a.ts;
      if (dt > 0) {
        upRate = (b.total_upload - a.total_upload) / dt;
        downRate = (b.total_download - a.total_download) / dt;
      }
    }
    const total = status.traffic_total || { upload: 0, download: 0 };
    const totalBytes = (total.upload || 0) + (total.download || 0);
    const sh = status.standby_history || [];
    const avgLatency = sh.length ? sh[sh.length - 1].avg : null;
    el.innerHTML = `
      <div class="flex justify-between"><span class="text-theme-muted">实时上行</span><span class="font-medium text-theme-accent">${fmtBytes(upRate)}/s</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">实时下行</span><span class="font-medium text-theme-success">${fmtBytes(downRate)}/s</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">累计流量</span><span class="font-medium">${fmtBytes(totalBytes)}</span></div>
      <div class="flex justify-between"><span class="text-theme-muted">平均延迟</span><span class="font-medium">${avgLatency != null ? avgLatency.toFixed(3) + 's' : '-'}</span></div>
    `;
  }

  function onStatus(status) {
    if (window.appState.currentPage !== 'dashboard') return;
    renderContent(status);
  }

  window.pageHandlers.dashboard = { render, onStatus };
})();
