<script setup lang="ts">
/**
 * 行内迷你趋势图：纯 SVG 折线（可多条序列，共用一条时间轴与 y 值域）。
 *
 * 刻意不用 ECharts——每行一个图表实例会带来 option 解析、组件体系、事件监听与
 * ResizeObserver 的成倍开销，几十行的列表里不可接受（见设计文档 §2）。
 * x 轴按时间戳映射、null 值断线（探测失败期间留出真实空白），y 轴自适应或对数。
 */
import { computed } from 'vue';
import { downsample, sparkSegments, type NumericPoint } from '@/utils/series';

const props = withDefaults(
  defineProps<{
    /** 曲线：pts 为原始序列（[时间戳, 值]，null 表示该点探测失败），color 为 CSS 变量值 */
    series: { pts: NumericPoint[]; color: string }[];
    /** 对数 y 轴（延迟跨数量级时更易读，与仪表盘延迟趋势图一致） */
    log?: boolean;
    height?: number;
  }>(),
  {
    log: false,
    height: 44,
  },
);

/** 坐标按 viewBox 的固定宽度算，实际宽度由容器拉伸（preserveAspectRatio=none） */
const VIEW_WIDTH = 320;

const view = computed(() => {
  const drawn = props.series.map((s) => downsample(s.pts));
  const hasValue = drawn.some((pts) => pts.some((p) => p[1] !== null));
  if (!hasValue) return null;
  const segments = sparkSegments(drawn, { width: VIEW_WIDTH, height: props.height, log: props.log });
  return props.series.map((s, i) => ({ color: s.color, segments: segments[i] }));
});
</script>

<template>
  <svg
    v-if="view"
    class="sparkline"
    :viewBox="`0 0 ${VIEW_WIDTH} ${height}`"
    :height="height"
    preserveAspectRatio="none"
    role="img"
  >
    <template v-for="(s, i) in view" :key="i">
      <polyline
        v-for="(seg, j) in s.segments"
        :key="j"
        :points="seg"
        fill="none"
        :stroke="s.color"
        stroke-width="1.5"
        stroke-linejoin="round"
        stroke-linecap="round"
        vector-effect="non-scaling-stroke"
      />
    </template>
  </svg>
  <div v-else class="sparkline-empty" :style="{ height: `${height}px` }">暂无采样</div>
</template>
