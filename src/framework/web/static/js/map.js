/**
 * IP 分布地图模块（嵌入仪表盘）：ECharts 世界地图 + 城市级散点（颜色深浅 + 点大小 = 密度）。
 * 数据通过 /api/geo/map 获取，后端按城市聚合后返回坐标点。
 * 由 dashboard.js 调用 window.geoMap.refresh()（内部 60 秒节流），懒加载 world.json 注册地图。
 * 暗色模式自适应：监听 document.documentElement class 变化重绘配色。
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
  // 保留 geo 组件的 roam 状态（缩放/平移），供全量重绘时恢复
  function captureRoamState() {
    var state = {};
    if (!mapChart) return state;
    try {
      var cur = mapChart.getOption();
      if (cur.geo && cur.geo[0]) {
        if (cur.geo[0].center) state.center = cur.geo[0].center;
        if (cur.geo[0].zoom) state.zoom = cur.geo[0].zoom;
      }
    } catch (e) { /* 忽略 */ }
    return state;
  }

  function applyRoamState(opt, state) {
    if (state.center) opt.geo.center = state.center;
    if (state.zoom) opt.geo.zoom = state.zoom;
    return opt;
  }

  function mapContainerEl() {
    return document.getElementById('dash-geo-map');
  }

  // 容器是否可见（页面 hidden 时 offsetParent 为 null 或尺寸为 0）
  function isMapVisible() {
    var el = mapContainerEl();
    if (!el) return false;
    return el.offsetParent !== null && el.offsetWidth > 0 && el.offsetHeight > 0;
  }

  // 销毁图表实例：容器不可见时调用，立即停掉 ECharts 动画循环，
  // 避免 0 尺寸 canvas 的 drawImage 疯狂报错（InvalidStateError）
  function disposeChart() {
    if (mapChart) {
      mapChart.dispose();
      mapChart = null;
    }
  }

  // 确保图表实例存在且容器可见；不可见时销毁并返回 false
  function ensureChart() {
    if (!isMapVisible()) {
      disposeChart();
      return false;
    }
    if (!mapChart) {
      mapChart = echarts.init(mapContainerEl());
    }
    return true;
  }

  function renderMap() {
    if (!geoData) return;
    if (!mapContainerEl()) return;
    if (!ensureChart()) return;
    var roam = captureRoamState();
    var opt = applyRoamState(buildOption(geoData), roam);
    mapChart.setOption(opt, true);
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
    if (!mapChart) return;
    if (!isMapVisible()) {
      disposeChart();
      return;
    }
    mapChart.resize();
  }

  // 主题切换时重绘地图配色（图表常驻仪表盘，需响应主题变化）
  var observer = new MutationObserver(function () {
    if (!isMapVisible()) {
      disposeChart();
      return;
    }
    if (mapChart && geoData) {
      var roam = captureRoamState();
      mapChart.setOption(applyRoamState(buildOption(geoData), roam), true);
    }
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });

  // 页面切换观察器：仪表盘 section 被加/移 hidden class 时销毁/重建图表，
  // 避免切走页面后图表动画在 0 尺寸 canvas 上持续绘制导致 InvalidStateError 刷屏
  var dashSection = document.getElementById('page-dashboard');
  if (dashSection) {
    var sectionObserver = new MutationObserver(function () {
      if (!isMapVisible()) {
        disposeChart();
      } else if (!mapChart && geoData) {
        renderMap();  // 切回仪表盘时重建图表（数据已缓存，直接渲染）
      }
    });
    sectionObserver.observe(dashSection, { attributes: true, attributeFilter: ['class'] });
  }

  window.addEventListener('resize', onResize);

  // 暴露给 dashboard.js：仪表盘渲染时调用（内部节流，不随 2s 心跳刷）
  window.geoMap = { refresh: refresh };
})();
