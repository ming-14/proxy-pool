<script setup lang="ts">
/**
 * 通用 ECharts 容器：挂载 init、option 变化整体替换、尺寸自适应、卸载 dispose。
 * 生命周期由 Vue 组件树保证，无需旧版 MutationObserver 可见性补丁。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { echarts, type EChartsOption, type EChartsType } from './echarts';

const props = defineProps<{ option: EChartsOption }>();

const el = ref<HTMLElement | null>(null);
let chart: EChartsType | null = null;
let ro: ResizeObserver | null = null;

onMounted(() => {
  chart = echarts.init(el.value!);
  chart.setOption(props.option, true);
  ro = new ResizeObserver(() => chart?.resize());
  ro.observe(el.value!);
});

watch(
  () => props.option,
  (opt) => chart?.setOption(opt, true),
  { deep: false },
);

onBeforeUnmount(() => {
  ro?.disconnect();
  chart?.dispose();
  chart = null;
});
</script>

<template>
  <div ref="el" class="w-full h-full" />
</template>
