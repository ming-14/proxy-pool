/**
 * 图表模块：代理服务队列延迟趋势与心跳健康率趋势。
 * 使用 Chart.js，数据由 /api/status/history 增量下发（app.js 维护 appState.history）。
 *
 * 渲染策略：增量追加（每 2s tick 只 push 新点，不再全量重建 labels/data/重算差分），
 * 避免 900+ 点 × 3 图的每轮 toLocaleTimeString 与全量 update 造成浏览器 CPU 周期高峰。
 * 全量重建仅发生在：历史被重置（页面刷新重拉全量 / 窗口滚动导致首点变化）。
 */
(function () {
  'use strict';

  let latencyChart = null;
  let healthChart = null;
  let trafficChart = null;

  // 各图表的增量渲染状态：len=上次渲染时 history 长度, firstTs=首点时间戳（全量重建判定）
  const renderState = {
    latency: { len: 0, firstTs: null, fmt: new Map() },
    health: { len: 0, firstTs: null, fmt: new Map() },
    traffic: { len: 0, firstTs: null, fmt: new Map(), prev: null }, // prev=上次差分基准（最后一个累计点）
  };

  function getCssVar(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim();
  }

  function createCanvas(id) {
    const wrapper = document.getElementById(id);
    if (!wrapper) return null;
    wrapper.innerHTML = '';
    const canvas = document.createElement('canvas');
    wrapper.appendChild(canvas);
    return canvas;
  }

  function chartColors() {
    return {
      primary: getCssVar('--primary-500') || '#3b82f6',
      primaryLight: getCssVar('--primary-200') || '#bfdbfe',
      success: getCssVar('--success') || '#10b981',
      warning: getCssVar('--warning') || '#f59e0b',
      danger: getCssVar('--danger') || '#ef4444',
      text: getCssVar('--text-secondary') || '#64748b',
      grid: getCssVar('--border-color') || '#e2e8f0',
    };
  }

  function commonOptions() {
    const c = chartColors();
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: c.text, usePointStyle: true, boxWidth: 8 } },
        tooltip: {
          backgroundColor: getCssVar('--bg-card'),
          titleColor: getCssVar('--text-primary'),
          bodyColor: c.text,
          borderColor: c.grid,
          borderWidth: 1,
          padding: 10,
          displayColors: true,
        },
      },
      scales: {
        x: {
          grid: { color: c.grid, drawBorder: false },
          ticks: { color: c.text, maxTicksLimit: 8 },
        },
        y: {
          grid: { color: c.grid, drawBorder: false },
          ticks: { color: c.text },
          beginAtZero: true,
        },
      },
    };
  }

  // 时间戳 → HH:MM:SS（x 轴刻度）；结果按 ts 缓存，增量渲染时只格式化新点
  function fmtTs(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('zh-CN', { hour12: false });
  }

  function fmtTsCached(cache, ts) {
    let s = cache.get(ts);
    if (s === undefined) {
      s = fmtTs(ts);
      cache.set(ts, s);
    }
    return s;
  }

  // 是否需全量重建：history 变短（重置/裁剪）或首点变化（刷新重拉全量）
  function needsRebuild(st, h) {
    return h.length < st.len || (h.length > 0 && st.firstTs !== h[0].ts);
  }

  // 全量重建后记录状态基线
  function setBaseline(st, h) {
    st.len = h.length;
    st.firstTs = h.length > 0 ? h[0].ts : null;
  }

  window.charts = {
    initLatency(id) {
      const canvas = createCanvas(id);
      if (!canvas) return;
      const c = chartColors();
      latencyChart = new Chart(canvas, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: '平均延迟',
              data: [],
              borderColor: c.primary,
              backgroundColor: c.primary + '20',
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              pointHoverRadius: 4,
            },
            {
              label: '最小延迟',
              data: [],
              borderColor: c.success,

              tension: 0.3,
              pointRadius: 0,
              pointHoverRadius: 3,
            },
            {
              label: '最大延迟',
              data: [],
              borderColor: c.warning,

              tension: 0.3,
              pointRadius: 0,
              pointHoverRadius: 3,
            },
          ],
        },
        options: {
          ...commonOptions(),
          scales: {
            x: commonOptions().scales.x,
            y: { type: 'logarithmic', grid: { color: c.grid, drawBorder: false }, ticks: { color: c.text } },
          },
        },
      });
    },

    initHealth(id) {
      const canvas = createCanvas(id);
      if (!canvas) return;
      const c = chartColors();
      healthChart = new Chart(canvas, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: '健康率 (%)',
              data: [],
              borderColor: c.success,
              backgroundColor: c.success + '20',
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              pointHoverRadius: 4,
              yAxisID: 'y',
            },
            {
              label: '队列长度',
              data: [],
              borderColor: c.primary,
              backgroundColor: c.primary + '15',
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              pointHoverRadius: 4,
              yAxisID: 'y1',
            },
          ],
        },
        options: {
          ...commonOptions(),
          scales: {
            x: commonOptions().scales.x,
            y: {
              ...commonOptions().scales.y,
              min: 0,
              max: 100,
              title: { display: true, text: '健康率 %' },
            },
            y1: {
              position: 'right',
              grid: { drawOnChartArea: false },
              ticks: { color: c.text },
              beginAtZero: true,
              title: { display: true, text: '数量' },
            },
          },
        },
      });
    },

    // 增量重绘：history 为完整累积序列（app.js 维护），只追加新点；全量重建仅在重置/刷新时
    renderLatency(history) {
      if (!latencyChart) return;
      const h = history || [];
      const st = renderState.latency;
      if (needsRebuild(st, h)) {
        st.fmt.clear();
        latencyChart.data.labels = h.map((p) => fmtTsCached(st.fmt, p.ts));
        latencyChart.data.datasets[0].data = h.map((p) => p.avg);
        latencyChart.data.datasets[1].data = h.map((p) => p.min);
        latencyChart.data.datasets[2].data = h.map((p) => p.max);
        setBaseline(st, h);
        latencyChart.update('none');
        return;
      }
      if (h.length === st.len) return; // 无新点
      for (let i = st.len; i < h.length; i++) {
        const p = h[i];
        latencyChart.data.labels.push(fmtTsCached(st.fmt, p.ts));
        latencyChart.data.datasets[0].data.push(p.avg);
        latencyChart.data.datasets[1].data.push(p.min);
        latencyChart.data.datasets[2].data.push(p.max);
      }
      st.len = h.length;
      latencyChart.update('none');
    },

    renderHealth(history) {
      if (!healthChart) return;
      const h = history || [];
      const st = renderState.health;
      if (needsRebuild(st, h)) {
        st.fmt.clear();
        healthChart.data.labels = h.map((p) => fmtTsCached(st.fmt, p.ts));
        // locked 期间采集的点：健康率/队列长度无意义，置 null 断开曲线
        healthChart.data.datasets[0].data = h.map((p) => p.locked ? null : p.health_rate);
        healthChart.data.datasets[1].data = h.map((p) => p.locked ? null : p.queue_length);
        setBaseline(st, h);
        healthChart.update('none');
        return;
      }
      if (h.length === st.len) return; // 无新点
      for (let i = st.len; i < h.length; i++) {
        const p = h[i];
        healthChart.data.labels.push(fmtTsCached(st.fmt, p.ts));
        healthChart.data.datasets[0].data.push(p.locked ? null : p.health_rate);
        healthChart.data.datasets[1].data.push(p.locked ? null : p.queue_length);
      }
      st.len = h.length;
      healthChart.update('none');
    },

    initTraffic(id) {
      const canvas = createCanvas(id);
      if (!canvas) return;
      const c = chartColors();
      trafficChart = new Chart(canvas, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: '上行',
              data: [],
              borderColor: c.primary,
              backgroundColor: c.primary + '20',
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              pointHoverRadius: 4,
            },
            {
              label: '下行',
              data: [],
              borderColor: c.success,
              backgroundColor: c.success + '20',
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              pointHoverRadius: 4,
            },
            {
              label: '平均延迟',
              data: [],
              borderColor: c.warning,

              tension: 0.3,
              pointRadius: 0,
              pointHoverRadius: 3,
              yAxisID: 'y1',
            },
          ],
        },
        options: {
          ...commonOptions(),
          scales: {
            x: commonOptions().scales.x,
            y: {
              type: 'logarithmic',
              grid: { color: c.grid, drawBorder: false },
              ticks: { color: c.text },
              title: { display: true, text: '字节/s' },
            },
            y1: {
              type: 'logarithmic',
              position: 'right',
              grid: { drawOnChartArea: false },
              ticks: { color: c.text },
              title: { display: true, text: '延迟 s' },
            },
          },
        },
      });
    },

    // 从累计字节序列差分算速率（相邻点差值/时间差），叠加平均延迟（右轴）。
    // 增量模式：只对新增的累计点差分（prev 记录上次基准），避免每轮全量重算。
    renderTraffic(history, latencyHistory) {
      if (!trafficChart) return;
      const h = history || [];
      const l = latencyHistory || [];
      const st = renderState.traffic;

      // 差分基准无效（重置/刷新）时全量重建
      if (needsRebuild(st, h)) {
        st.fmt.clear();
        st.prev = null;
        trafficChart.data.labels = [];
        trafficChart.data.datasets[0].data = [];
        trafficChart.data.datasets[1].data = [];
        trafficChart.data.datasets[2].data = [];
        st.len = 0;
        st.firstTs = h.length > 0 ? h[0].ts : null; // 必须同步基线，否则每轮被判全量重建
      }
      if (h.length === st.len) return; // 无新点

      for (let i = st.len; i < h.length; i++) {
        const cur = h[i];
        if (st.prev !== null) {
          const dt = cur.ts - st.prev.ts;
          if (dt > 0) {
            trafficChart.data.labels.push(fmtTsCached(st.fmt, cur.ts));
            trafficChart.data.datasets[0].data.push((cur.total_upload - st.prev.total_upload) / dt);
            trafficChart.data.datasets[1].data.push((cur.total_download - st.prev.total_download) / dt);
            trafficChart.data.datasets[2].data.push(l[i] ? l[i].avg : null);
          }
        }
        st.prev = cur;
      }
      st.len = h.length;
      trafficChart.update('none');
    },

    updateTheme() {
      [latencyChart, healthChart, trafficChart].forEach((chart) => {
        if (!chart) return;
        const c = chartColors();
        chart.options.plugins.legend.labels.color = c.text;
        chart.options.plugins.tooltip.backgroundColor = getCssVar('--bg-card');
        chart.options.plugins.tooltip.titleColor = getCssVar('--text-primary');
        chart.options.plugins.tooltip.bodyColor = c.text;
        chart.options.plugins.tooltip.borderColor = c.grid;
        ['x', 'y', 'y1'].forEach((axis) => {
          const scale = chart.options.scales[axis];
          if (scale) {
            scale.grid.color = c.grid;
            scale.ticks.color = c.text;
          }
        });
        chart.update('none');
      });
    },

    // 工厂函数：为 hover tooltip 创建独立的延时+流量图表实例（不与全局单例冲突）
    createProxyCharts(container) {
      const c = chartColors();
      container.innerHTML = `
        <div class="proxy-tooltip-section">
          <div class="proxy-tooltip-label">延时趋势</div>
          <div class="proxy-tooltip-chart" style="height:120px"></div>
        </div>
        <div class="proxy-tooltip-section">
          <div class="proxy-tooltip-label">流量速率</div>
          <div class="proxy-tooltip-chart" style="height:120px"></div>
        </div>
      `;
      const chartEls = container.querySelectorAll('.proxy-tooltip-chart');

      // 延时图：单条线（latency），对数 y 轴避免高延时尖峰压扁正常值
      const latencyC = new Chart(chartEls[0].appendChild(document.createElement('canvas')), {
        type: 'line',
        data: {
          labels: [],
          datasets: [{
            label: '延迟',
            data: [],
            borderColor: c.primary,
            backgroundColor: c.primary + '20',
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            pointHoverRadius: 4,
          }],
        },
        options: {
          ...commonOptions(),
          plugins: { ...commonOptions().plugins, legend: { display: false } },
          scales: {
            x: commonOptions().scales.x,
            y: { type: 'logarithmic', grid: { color: c.grid, drawBorder: false }, ticks: { color: c.text } },
          },
        },
      });

      // 流量图：上下行速率（差分算），不叠加平均延迟
      const trafficC = new Chart(chartEls[1].appendChild(document.createElement('canvas')), {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            { label: '上行', data: [], borderColor: c.primary, backgroundColor: c.primary + '20', fill: true, tension: 0.35, pointRadius: 0, pointHoverRadius: 4 },
            { label: '下行', data: [], borderColor: c.success, backgroundColor: c.success + '20', fill: true, tension: 0.35, pointRadius: 0, pointHoverRadius: 4 },
          ],
        },
        options: {
          ...commonOptions(),
          plugins: { ...commonOptions().plugins, legend: { labels: { color: c.text, usePointStyle: true, boxWidth: 8 } } },
          scales: {
            x: commonOptions().scales.x,
            y: { ...commonOptions().scales.y, title: { display: true, text: 'B/s' } },
          },
        },
      });

      return {
        render(latencyHistory, trafficHistory) {
          // 延时图：{ts, latency} 序列（对数 y 轴，高值按比例压缩，不裁剪）
          const lh = latencyHistory || [];
          latencyC.data.labels = lh.map((p) => fmtTs(p.ts));
          latencyC.data.datasets[0].data = lh.map((p) => p.latency);
          latencyC.update('none');

          // 流量图：累计字节序列差分算速率
          const th = trafficHistory || [];
          const rates = [];
          for (let i = 1; i < th.length; i++) {
            const dt = th[i].ts - th[i - 1].ts;
            if (dt <= 0) continue;
            rates.push({
              ts: th[i].ts,
              up: (th[i].total_upload - th[i - 1].total_upload) / dt,
              down: (th[i].total_download - th[i - 1].total_download) / dt,
            });
          }
          trafficC.data.labels = rates.map((r) => fmtTs(r.ts));
          trafficC.data.datasets[0].data = rates.map((r) => r.up);
          trafficC.data.datasets[1].data = rates.map((r) => r.down);
          trafficC.update('none');
        },
        destroy() {
          latencyC.destroy();
          trafficC.destroy();
        },
      };
    },
  };

  // 监听主题切换以重绘图表颜色
  const observer = new MutationObserver(() => {
    window.charts.updateTheme();
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
})();
