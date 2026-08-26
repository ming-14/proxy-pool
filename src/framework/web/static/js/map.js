/**
 * IP 分布地图模块（嵌入仪表盘）：ECharts 世界地图 + 城市级散点（颜色深浅 + 点大小 = 密度）。
 * 数据通过 /api/geo/map 获取，后端按城市聚合后返回坐标点。
 * 由 dashboard.js 调用 window.geoMap.refresh()（内部 60 秒节流），懒加载 world.json 注册地图。
 * 暗色模式自适应：监听 document.documentElement class 变化重绘配色。
 * 国家名显示映射（中国 → 中国大陆 / China → China's Mainland）仅影响展示。
 */
(function () {
  'use strict';

  var mapChart = null;
  var geoData = null;
  var mapRegistered = false;
  var lastRefresh = 0;
  var REFRESH_THROTTLE_MS = 60000;
  var MAP_JSON_URL = '/static/vendor/world.json';

  function getCssVar(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim();
  }

  function isDark() {
    return document.documentElement.classList.contains('dark');
  }

  // 国家名显示映射统一走 app.js 的 utils.displayCountryName（地图 + 代理组共用）

  // ---------------------------------------------------------------- 地图加载（懒加载，全局只注册一次）
  function loadMap() {
    if (mapRegistered) return Promise.resolve();
    return fetch(MAP_JSON_URL)
      .then(function (r) { return r.json(); })
      .then(function (json) {
        echarts.registerMap('world', json);
        mapRegistered = true;
      });
  }

  // ---------------------------------------------------------------- 统计文本（地图卡片标题右侧）
  function renderStats(data) {
    var el = document.getElementById('dash-geo-stats');
    if (!el) return;
    el.textContent = '总 IP ' + data.total_ips + ' · 已解析 ' + data.resolved_ips
      + ' · 未解析 ' + data.unresolved_ips + ' · 覆盖国家/地区 ' + data.countries.length;
  }

  // ---------------------------------------------------------------- ECharts 地图选项
  function buildOption(data) {
    var dark = isDark();
    var c = getCssVar('--text-secondary') || '#64748b';
    var bgColor = dark ? '#1e293b' : '#f8fafc';
    var borderColor = dark ? '#334155' : '#cbd5e1';
    var maxCount = 1;
    if (data.points && data.points.length) {
      maxCount = data.points.reduce(function (m, p) { return Math.max(m, p.count); }, 1);
    }
    var visualColors = dark
      ? ['#1e3a5f', '#3b82f6', '#60a5fa', '#a78bfa']
      : ['#e0f3ff', '#3b82f6', '#1e3a8a', '#7c3aed'];

    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        formatter: function (params) {
          if (params.seriesType === 'effectScatter' || params.seriesType === 'scatter') {
            var d = params.data;
            return '<strong>' + utils.esc(utils.displayCountryName(d.name)) + '</strong><br/>'
              + 'IP 数: ' + d.count + '<br/>'
              + (d.country ? utils.esc(utils.displayCountryName(d.country)) : '');
          }
          return utils.displayCountryName(params.name);
        },
      },
      visualMap: {
        min: 1,
        max: maxCount,
        dimension: 2,
        calculable: true,
        left: 'left',
        bottom: 20,
        textStyle: { color: c },
        inRange: { color: visualColors },
        text: ['密集', '稀疏'],
      },
      geo: {
        map: 'world',
        roam: true,
        label: { show: false },
        itemStyle: {
          areaColor: bgColor,
          borderColor: borderColor,
          borderWidth: 0.5,
        },
        emphasis: {
          itemStyle: { areaColor: dark ? '#334155' : '#e2e8f0' },
        },
      },
      series: [{
        type: 'effectScatter',
        coordinateSystem: 'geo',
        zlevel: 2,
        rippleEffect: { brushType: 'stroke', scale: 3 },
        label: { show: false },
        symbolSize: function (val) {
          var count = val[2] || 1;
          return 3 + Math.sqrt(count) * 4;
        },
        data: (data.points || []).map(function (p) {
          return {
            name: p.name,
            value: [p.lng, p.lat, p.count],
            count: p.count,
            country: p.country,
          };
        }),
        itemStyle: {
          color: dark ? '#60a5fa' : '#3b82f6',
          opacity: 0.8,
        },
      }],
    };
  }

  // ---------------------------------------------------------------- 渲染
  function renderMap() {
    if (!geoData) return;
    var el = document.getElementById('dash-geo-map');
    if (!el) return;
    if (!mapChart) {
      mapChart = echarts.init(el);
    }
    mapChart.setOption(buildOption(geoData), true);
    mapChart.resize();
  }

  // ---------------------------------------------------------------- 刷新（懒加载 + 节流）
  function refresh() {
    var now = Date.now();
    if (now - lastRefresh < REFRESH_THROTTLE_MS) return;
    lastRefresh = now;
    loadMap().then(function () {
      return window.api.geoMap();
    }).then(function (data) {
      geoData = data;
      renderStats(data);
      renderMap();
    }).catch(function (err) {
      console.error('IP 分布地图加载失败', err);
      showToast('error', 'IP 分布地图加载失败', err.message || '请检查网络');
    });
  }

  function onResize() {
    if (mapChart) mapChart.resize();
  }

  // 主题切换时重绘地图配色（图表常驻仪表盘，需响应主题变化）
  var observer = new MutationObserver(function () {
    if (mapChart && geoData) {
      mapChart.setOption(buildOption(geoData), true);
    }
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });

  window.addEventListener('resize', onResize);

  // 暴露给 dashboard.js：仪表盘渲染时调用（内部节流，不随 2s 心跳刷）
  window.geoMap = { refresh: refresh };
})();