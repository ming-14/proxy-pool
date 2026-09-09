<script setup lang="ts">
/**
 * IP 分布地图卡片：ECharts geo 世界地图 + 城市散点（颜色深浅+大小=密度）。
 *
 * - world.json 懒加载且全局仅 registerMap 一次；数据 60s 节流（不随 2s 心跳刷）。
 * - roam（缩放/平移）状态在数据/主题重绘间保留 → 自行管理实例，不走 ChartBox。
 * - 组件卸载即 dispose（切页销毁由 Vue 生命周期天然保证，替代旧版双重
 *   MutationObserver 补丁）；波纹动画默认关（effectScatter 全帧重绘致 CPU 高企）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { echarts, type EChartsOption, type EChartsType } from './echarts';
import { EffectScatterChart } from 'echarts/charts';
import { useThemeStore } from '@/stores/theme';
import { useStatusStore } from '@/stores/status';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import { displayCountryName, escapeHtml } from '@/utils/format';
import type { GeoMapData } from '@/types/api';

echarts.use([EffectScatterChart]);

const REFRESH_THROTTLE_MS = 60_000;
const RIPPLE_ENABLED = false; // 演示用开启：CPU 显著升高，仅短期
const RIPPLE_TOP_N = 3;

// 模块级：world.json 全局仅加载并注册一次（页面往返不重复拉取 1MB）
let mapPromise: Promise<void> | null = null;

const themeStore = useThemeStore();
const statusStore = useStatusStore();
const toast = useToastStore();

const el = ref<HTMLElement | null>(null);
const statsText = ref('');

let chart: EChartsType | null = null;
let geoData: GeoMapData | null = null;
let lastRefresh = 0;
let resizeHandler: (() => void) | null = null;

function loadWorldMap(): Promise<void> {
  if (!mapPromise) {
    mapPromise = fetch('/world.json')
      .then((r) => r.json())
      .then((json) => echarts.registerMap('world', json));
  }
  return mapPromise;
}

function isDark() {
  return themeStore.theme === 'dark';
}

function buildOption(data: GeoMapData): EChartsOption {
  const dark = isDark();
  const cs = getComputedStyle(document.documentElement);
  const textColor = cs.getPropertyValue('--color-t-secondary').trim() || '#64748b';
  const bgColor = dark ? '#1e293b' : '#f8fafc';
  const borderColor = dark ? '#334155' : '#cbd5e1';
  const visualColors = dark
    ? ['#1e3a5f', '#3b82f6', '#60a5fa', '#a78bfa']
    : ['#e0f3ff', '#3b82f6', '#1e3a8a', '#7c3aed'];
  const maxCount = data.points.reduce((m, p) => Math.max(m, p.count), 1);

  const all = data.points.map((p) => ({
    name: p.name,
    value: [p.lng, p.lat, p.count],
    count: p.count,
    country: p.country,
  }));
  const sorted = all.slice().sort((a, b) => b.count - a.count);
  const ripplePoints = RIPPLE_ENABLED ? sorted.slice(0, RIPPLE_TOP_N) : [];
  const staticPoints = RIPPLE_ENABLED ? sorted.slice(RIPPLE_TOP_N) : sorted;

  const symbolSize = (val: number[]) => 3 + Math.sqrt(val[2] || 1) * 4;
  const itemColor = dark ? '#60a5fa' : '#3b82f6';

  const series: EChartsOption['series'] = [
    {
      type: 'scatter',
      coordinateSystem: 'geo',
      zlevel: 1,
      label: { show: false },
      symbolSize,
      data: staticPoints,
      itemStyle: { color: itemColor, opacity: 0.8 },
    },
  ];
  if (RIPPLE_ENABLED) {
    (series as unknown[]).unshift({
      type: 'effectScatter',
      coordinateSystem: 'geo',
      zlevel: 2,
      rippleEffect: { brushType: 'stroke', scale: 3 },
      label: { show: false },
      symbolSize,
      data: ripplePoints,
      itemStyle: { color: itemColor, opacity: 0.8 },
    });
  }

  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        if (params.seriesType === 'effectScatter' || params.seriesType === 'scatter') {
          const d = params.data;
          // formatter 返回 HTML，数据文本必须显式转义（ECharts 不经 Vue 模板）
          return (
            `<strong>${escapeHtml(displayCountryName(d.name))}</strong><br/>` +
            `IP 数: ${d.count}<br/>` +
            (d.country ? escapeHtml(displayCountryName(d.country)) : '')
          );
        }
        return escapeHtml(displayCountryName(params.name));
      },
    },
    visualMap: {
      min: 1,
      max: maxCount,
      dimension: 2,
      calculable: true,
      left: 'left',
      bottom: 20,
      textStyle: { color: textColor },
      inRange: { color: visualColors },
      text: ['密集', '稀疏'],
    },
    geo: {
      map: 'world',
      roam: true,
      label: { show: false },
      itemStyle: { areaColor: bgColor, borderColor, borderWidth: 0.5 },
      emphasis: { itemStyle: { areaColor: dark ? '#334155' : '#e2e8f0' } },
    },
    series,
  };
}

/** 重绘并保留 roam 状态（用户缩放/平移不被数据刷新打断） */
function renderMap() {
  if (!chart || !geoData) return;
  // 注意：echarts getOption() 在首次 setOption 前返回 undefined（_model 未建）
  const prev = chart.getOption() as { geo?: { center?: unknown; zoom?: number }[] } | undefined;
  const opt = buildOption(geoData);
  const geoOpt = (opt as { geo: Record<string, unknown> }).geo;
  if (prev?.geo?.[0]) {
    if (prev.geo[0].center) geoOpt.center = prev.geo[0].center;
    if (prev.geo[0].zoom) geoOpt.zoom = prev.geo[0].zoom;
  }
  chart.setOption(opt, true);
}

function renderStats(data: GeoMapData) {
  statsText.value =
    `总 IP ${data.total_ips} · 已解析 ${data.resolved_ips}` +
    ` · 未解析 ${data.unresolved_ips} · 覆盖国家/地区 ${data.countries.length}`;
}

async function refresh(force = false) {
  const now = Date.now();
  if (!force && now - lastRefresh < REFRESH_THROTTLE_MS) return;
  lastRefresh = now;
  try {
    await loadWorldMap();
    const data = await api.geoMap();
    geoData = data;
    renderStats(data);
    renderMap();
  } catch (err) {
    console.error('IP 分布地图加载失败', err);
    toast.error('IP 分布地图加载失败', err instanceof Error ? err.message : '请检查网络');
  }
}

onMounted(async () => {
  chart = echarts.init(el.value!);
  if (geoData) renderMap();
  await refresh(true);
  resizeHandler = () => chart?.resize();
  window.addEventListener('resize', resizeHandler);
  // 状态心跳（2s）驱动节流刷新（与旧版 dashboard onStatus→refresh 等价）；
  // 主题变化重绘配色。setup 作用域内 watch 随组件卸载自动停止。
  watch(() => statusStore.status, () => refresh());
  watch(() => themeStore.theme, () => renderMap());
});

onBeforeUnmount(() => {
  if (resizeHandler) window.removeEventListener('resize', resizeHandler);
  chart?.dispose();
  chart = null;
});
</script>

<template>
  <div data-testid="geo-card" class="card p-4">
    <div class="flex items-center justify-between mb-4 flex-wrap gap-2">
      <div class="card-title">IP 分布地图</div>
      <div data-testid="geo-stats" class="text-xs text-[var(--color-t-muted)]">{{ statsText }}</div>
    </div>
    <div ref="el" data-testid="geo-map" style="height: 480px" />
  </div>
</template>
