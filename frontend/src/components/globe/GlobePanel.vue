<script setup lang="ts">
/**
 * 3D 地球卡片：代理节点 + 本机→代理/代理→目标城市的流量飞线（globe.gl）。
 *
 * - globe.gl（含 three 链）在卡片进入视口后才加载：主 chunk 与首屏不背 three；
 *   初始化失败（WebGL 不可用/构造异常）渲染降级文案，不影响面板其余功能。
 * - 球面样式（贴图 / 经纬网 / 大气光晕）由设置页「地球样式」决定，见 stores/globeStyle：
 *   默认真实地球贴图、无网格、无光晕（浅色卡片上深色贴图会显成暗球、默认光晕是一圈蓝雾）；
 *   不用 ECharts 的 world.json 做矢量地球：省掉 ~1MB 地图拉取，也没有多边形三角化的开销。
 * - 不可见（IntersectionObserver）或页面切后台（document.hidden）时暂停动画并停 5s
 *   轮询——没有人看时省 CPU 与请求；地球只消费最终数据，与后端 DNS 探测无关。
 * - 主题切换读 CSS 变量重建配色；容器尺寸由 ResizeObserver 跟随。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { useThemeStore } from '@/stores/theme';
import { globeTextureUrl, useGlobeStyleStore } from '@/stores/globeStyle';
import {
  ARC_DOT_STYLE,
  buildPoints,
  buildProxyArcs,
  buildTargetArcs,
  type GlobePalette,
} from './globeData';
import type { GlobeData } from '@/types/api';
import type { GlobeInstance } from 'globe.gl';

const POLL_INTERVAL_MS = 5_000;
const CARD_HEIGHT_PX = 420;
/** 初始视角：东亚上空，altitude 1.8 ≈ 球体占满卡片可视高度的 3/4（本机坐标只决定点位，不移动镜头） */
const INITIAL_POV = { lat: 30, lng: 110, altitude: 1.8 };

const themeStore = useThemeStore();
const globeStyle = useGlobeStyleStore();

const el = ref<HTMLElement | null>(null);
const failed = ref(false);
const ready = ref(false);
const statsText = ref('');
const showSelfHint = ref(false);

let world: GlobeInstance | null = null;
let initPromise: Promise<void> | null = null;
let io: IntersectionObserver | null = null;
let ro: ResizeObserver | null = null;
let pollTimer: number | null = null;
let intersecting = false;
let lastData: GlobeData | null = null;
let palette: GlobePalette = readPalette();

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** 数据层配色：统一读 CSS 变量（与 GeoMap 取色同口径），仅变量缺失时退回字面量 */
function readPalette(): GlobePalette {
  const dark = themeStore.theme === 'dark';
  return {
    self: cssVar('--color-info', dark ? '#22d3ee' : '#06b6d4'),
    proxy: cssVar('--color-primary-600', dark ? '#60a5fa' : '#2563eb'),
    locked: cssVar('--color-warning', '#f59e0b'),
    failed: cssVar('--color-danger', '#ef4444'),
    target: cssVar('--color-success', '#10b981'),
  };
}

/** 应用球面样式（贴图/经纬网/大气光晕）：初始化与设置页改动共用一处 */
function applySurfaceStyle(): void {
  if (!world) return;
  const { texture, graticules, atmosphere } = globeStyle.style;
  world
    .globeImageUrl(globeTextureUrl(texture))
    .showGraticules(graticules)
    .showAtmosphere(atmosphere);
}

function applyData(data: GlobeData): void {
  lastData = data;
  statsText.value = `代理节点 ${data.nodes.length} · 目标弧 ${data.flows.length}`;
  showSelfHint.value = data.self === null;
  if (!world) return;
  world
    .pointsData(buildPoints(data, palette))
    .arcsData([...buildProxyArcs(data, palette), ...buildTargetArcs(data, palette)]);
}

async function refresh(): Promise<void> {
  try {
    applyData(await api.globe());
  } catch (err) {
    // 5s 轮询失败保持上一帧：不在卡片上弹 toast（持续失败会刷屏），只记控制台
    console.error('3D 地球数据加载失败', err);
  }
}

function startPolling(): void {
  if (pollTimer !== null || !ready.value) return;
  void refresh();
  pollTimer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** 可见且前台才算活跃：恢复动画+轮询；否则暂停并停轮询（省 CPU 与请求） */
function updateActivity(): void {
  if (intersecting && !document.hidden) {
    world?.resumeAnimation();
    startPolling();
  } else {
    world?.pauseAnimation();
    stopPolling();
  }
}

/** 首次可见时初始化 globe（模块动态 import：three 链不与面板主 chunk 同包） */
function ensureGlobe(): Promise<void> {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    try {
      const { default: Globe } = await import('globe.gl');
      const node = el.value; // 加载期间组件可能已卸载：此时不再建实例
      if (!node) return;
      const globe = new Globe(node);
      globe
        .width(node.clientWidth)
        .height(CARD_HEIGHT_PX)
        .backgroundColor('rgba(0,0,0,0)')
        .pointLat('lat').pointLng('lng').pointColor('color')
        .pointRadius('radius').pointAltitude('altitude').pointLabel('label')
        .arcStartLat('startLat').arcStartLng('startLng')
        .arcEndLat('endLat').arcEndLng('endLng')
        .arcColor('color').arcStroke('stroke')
        .arcDashLength(ARC_DOT_STYLE.dashLength).arcDashGap(ARC_DOT_STYLE.dashGap)
        .arcDashAnimateTime('dashTime').arcLabel('label')
        .pointOfView(INITIAL_POV);
      world = globe;
      applySurfaceStyle();
      ready.value = true;
      if (lastData) applyData(lastData);
      updateActivity();
    } catch (err) {
      failed.value = true;
      console.error('3D 地球初始化失败（WebGL 不可用？）', err);
    }
  })();
  return initPromise;
}

// 主题切换重建配色（点位/弧的颜色烘焙在数据里，重建数据即刷新；球面为贴图不随主题变）
watch(() => themeStore.theme, () => {
  palette = readPalette();
  if (lastData) applyData(lastData);
});

// 设置页「地球样式」改动即时生效（贴图/网格/光晕均为 kapsule prop，赋值即重绘）
watch(
  () => [globeStyle.style.texture, globeStyle.style.graticules, globeStyle.style.atmosphere],
  () => applySurfaceStyle(),
);

onMounted(() => {
  const node = el.value!;
  io = new IntersectionObserver((entries) => {
    intersecting = entries.some((entry) => entry.isIntersecting);
    if (intersecting && !world) void ensureGlobe();
    else updateActivity();
  });
  io.observe(node);
  ro = new ResizeObserver(() => {
    if (world && el.value) world.width(el.value.clientWidth).height(CARD_HEIGHT_PX);
  });
  ro.observe(node);
  document.addEventListener('visibilitychange', updateActivity);
});

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', updateActivity);
  stopPolling();
  io?.disconnect();
  io = null;
  ro?.disconnect();
  ro = null;
  world?._destructor();
  world = null;
  ready.value = false;
});
</script>

<template>
  <div data-testid="globe-card" class="card p-4">
    <div class="flex items-center justify-between mb-4 flex-wrap gap-2">
      <div class="card-title">3D 地球</div>
      <div data-testid="globe-stats" class="text-xs text-[var(--color-t-muted)]">{{ statsText }}</div>
    </div>
    <div
      v-if="failed"
      data-testid="globe-fallback"
      class="flex items-center justify-center text-sm text-[var(--color-t-muted)]"
      :style="{ height: `${CARD_HEIGHT_PX}px` }"
    >
      当前环境不支持 WebGL，3D 地球不可用
    </div>
    <!-- 高度由 JS 常量驱动（与 globe.height()/RO 同一来源），避免模板里再写一份字面量 -->
    <div v-else ref="el" data-testid="globe-canvas" class="w-full" :style="{ height: `${CARD_HEIGHT_PX}px` }" />
    <div v-if="showSelfHint && !failed" class="mt-2 text-xs text-[var(--color-t-muted)]">
      未配置本机坐标：在 config/panel.toml 的 [globe] 填 self_lat / self_lng 后显示「本机 → 代理」飞线
    </div>
  </div>
</template>
