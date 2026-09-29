<script setup lang="ts">
/**
 * 行内迷你趋势图：纯 SVG 折线（可多条序列，共用一条时间轴与 y 值域）。
 *
 * 刻意不用 ECharts——每行一个图表实例会带来 option 解析、组件体系、事件监听与
 * ResizeObserver 的成倍开销，几十行的列表里不可接受（见设计文档 §2）。
 * x 轴按时间戳映射、null 值断线（探测失败/零流量期间留出真实空白），y 轴自适应或对数。
 * 时间窗口由父级下发（全部行共用）：各行自己归一化的话，刚入队只有几点的行与跑了
 * 半小时的行看着一样，采样已停止的行也会贴到右边缘装成最新。
 * 传 formatValue 时在左侧标出该图的数据值区间（上=最大、下=最小）：迷你图没有坐标轴，
 * 不标出来就读不出量级；文本用 HTML 渲染（SVG 被 preserveAspectRatio=none 拉伸，文字会变形）。
 */
import { computed } from 'vue';
import { downsample, sparkDomain, sparkSegments, type NumericPoint } from '@/utils/series';

const props = withDefaults(
  defineProps<{
    /** 曲线：pts 为原始序列（[时间戳, 值]，null 表示该点探测失败），color 为 CSS 变量值 */
    series: { pts: NumericPoint[]; color: string }[];
    /** 对数 y 轴（延迟跨数量级时更易读，与仪表盘延迟趋势图一致） */
    log?: boolean;
    height?: number;
    /** 共享时间窗口 [起, 止]（秒），由页面统一计算（见 utils/series.ts 的 seriesTimeWindow） */
    window: [number, number];
    /** 值文本格式化器；提供时左侧标出最大/最小数据值 */
    formatValue?: (v: number) => string;
    /** 无任何可绘制点时的占位文案（按语义区分：无采样 / 无速率） */
    emptyText?: string;
  }>(),
  {
    log: false,
    height: 44,
    emptyText: '暂无采样',
  },
);

/** 坐标按 viewBox 的固定宽度算，实际宽度由容器拉伸（preserveAspectRatio=none） */
const VIEW_WIDTH = 320;

const drawn = computed(() => props.series.map((s) => downsample(s.pts)));

/**
 * y 值域：坐标映射与左侧数据值共用同一份。
 *
 * 必须只算一次——它要遍历全部点，而队列页每行都有实例、且随 2s 节拍重算；
 * sparkSegments 内部原本还会再算一遍，等于同样的遍历做两次。
 * 无任何可绘制点（全 null / 空）时为 null。
 */
const domain = computed(() => sparkDomain(drawn.value));

const view = computed(() => {
  const series = drawn.value;
  const d = domain.value;
  if (!d) return null;
  const segments = sparkSegments(series, {
    width: VIEW_WIDTH,
    height: props.height,
    log: props.log,
    window: props.window,
    domain: d,
  });
  return props.series.map((s, i) => ({ color: s.color, segments: segments[i] }));
});

/** 左侧数据值区间：与坐标映射同源（复用 domain），有数据且给了格式化器才显示 */
const scale = computed(() => {
  const d = domain.value;
  if (!props.formatValue || !d) return null;
  return { max: props.formatValue(d.hi), min: props.formatValue(d.lo) };
});
</script>

<template>
  <div class="sparkline-wrap">
    <!-- 只要给了格式化器就占住这一列（哪怕没数据）：否则绘图区会在有无数据的行之间错位，
         同一行的延时图与流量图也就对不齐，横向比较失去意义 -->
    <div v-if="formatValue" class="sparkline-scale" :style="{ height: `${height}px` }">
      <span>{{ scale?.max ?? '' }}</span>
      <span>{{ scale?.min ?? '' }}</span>
    </div>
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
    <div v-else class="sparkline-empty" :style="{ height: `${height}px` }">{{ emptyText }}</div>
  </div>
</template>
