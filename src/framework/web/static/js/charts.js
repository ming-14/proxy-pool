/**
 * 图表模块：代理服务队列延迟趋势与心跳健康率趋势。
 * 使用 Chart.js，数据由后端 /api/status 的 standby_history 下发，前端无状态重绘
 * （刷新页面不丢失，多标签一致；后端按时间窗口采样，超出窗口的自动清理）。
 */
(function () {
  'use strict';

  let latencyChart = null;
  let healthChart = null;
  let trafficChart = null;

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

  // 时间戳 → HH:MM:SS（x 轴刻度）
  function fmtTs(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('zh-CN', { hour12: false });
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

    // 用后端下发的完整 history 重绘（无本地累积状态，刷新即恢复）
    renderLatency(history) {
      if (!latencyChart) return;
      const h = history || [];
      latencyChart.data.labels = h.map((p) => fmtTs(p.ts));
      latencyChart.data.datasets[0].data = h.map((p) => p.avg);
      latencyChart.data.datasets[1].data = h.map((p) => p.min);
      latencyChart.data.datasets[2].data = h.map((p) => p.max);
      latencyChart.update('none');
    },

    renderHealth(history) {
      if (!healthChart) return;
      const h = history || [];
      healthChart.data.labels = h.map((p) => fmtTs(p.ts));
      // locked 期间采集的点：健康率/队列长度无意义，置 null 断开曲线
      healthChart.data.datasets[0].data = h.map((p) => p.locked ? null : p.health_rate);
      healthChart.data.datasets[1].data = h.map((p) => p.locked ? null : p.queue_length);
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

    // 从累计字节序列算速率（相邻点差值/时间差），叠加平均延迟（右轴），重绘
    renderTraffic(history, latencyHistory) {
      if (!trafficChart) return;
      const h = history || [];
      const l = latencyHistory || [];
      const rates = [];
      for (let i = 1; i < h.length; i++) {
        const dt = h[i].ts - h[i - 1].ts;
        if (dt <= 0) continue;
        rates.push({
          ts: h[i].ts,
          up: (h[i].total_upload - h[i - 1].total_upload) / dt,
          down: (h[i].total_download - h[i - 1].total_download) / dt,
          latency: l[i] ? l[i].avg : null,
        });
      }
      trafficChart.data.labels = rates.map((r) => fmtTs(r.ts));
      trafficChart.data.datasets[0].data = rates.map((r) => r.up);
      trafficChart.data.datasets[1].data = rates.map((r) => r.down);
      trafficChart.data.datasets[2].data = rates.map((r) => r.latency);
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
