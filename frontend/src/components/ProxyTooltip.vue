<script setup lang="ts">
/**
 * 代理统计浮窗：延时趋势 + 流量速率（ECharts）+ CF 测速。
 * 状态全部来自 useProxyTooltip 模块级单例；位置优先鼠标右侧，放不下翻转左侧。
 */
import { computed, nextTick, ref, watch } from 'vue';
import ChartBox from '@/components/charts/ChartBox.vue';
import { lineChartOption } from '@/components/charts/chartOptions';
import { useLineTheme } from '@/components/charts/useLineTheme';
import {
  speedTestState,
  tooltipHistory,
  tooltipState,
  hideTooltip,
  runSpeedTest,
  scheduleHide,
  cancelHide,
} from '@/composables/useProxyTooltip';
import { fmtBytes, fmtTime } from '@/utils/format';

const el = ref<HTMLElement | null>(null);
const theme = useLineTheme();

const latencyOption = computed(() => {
  const lh = tooltipHistory.value?.latency_history ?? [];
  return lineChartOption(
    {
      labels: lh.map((p) => fmtTime(p.ts)),
      series: [{ name: '延迟', data: lh.map((p) => p.latency), color: theme.value.primary, fillAlpha: '20' }],
      axes: [{ type: 'log' }],
      legend: false, // 参考图：延时趋势无图例
    },
    theme.value,
  );
});

const trafficOption = computed(() => {
  const th = tooltipHistory.value?.traffic_history ?? [];
  const labels: string[] = [];
  const up: number[] = [];
  const down: number[] = [];
  for (let i = 1; i < th.length; i++) {
    const dt = th[i].ts - th[i - 1].ts;
    if (dt <= 0) continue;
    labels.push(fmtTime(th[i].ts));
    up.push((th[i].total_upload - th[i - 1].total_upload) / dt);
    down.push((th[i].total_download - th[i - 1].total_download) / dt);
  }
  return lineChartOption(
    {
      labels,
      series: [
        { name: '上行', data: up, color: theme.value.primary, fillAlpha: '20' },
        { name: '下行', data: down, color: theme.value.success, fillAlpha: '20' },
      ],
      axes: [{ type: 'value', name: 'B/s' }],
    },
    theme.value,
  );
});

const statsText = computed(() => {
  const h = tooltipHistory.value;
  if (!h) return '加载中...';
  const lh = h.latency_history;
  const th = h.traffic_history;
  const lastLat = lh.length ? lh[lh.length - 1].latency : null;
  const totalDown = th.length ? th[th.length - 1].total_download : 0;
  const totalUp = th.length ? th[th.length - 1].total_upload : 0;
  return `延迟 ${lastLat != null ? lastLat.toFixed(3) + 's' : '-'} · ↑${fmtBytes(totalUp)} ↓${fmtBytes(totalDown)}`;
});

async function place() {
  const node = el.value;
  if (!node) return;
  node.style.visibility = 'hidden';
  await nextTick();
  const rect = node.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 8;
  const maxY = window.innerHeight - rect.height - 8;
  let x = tooltipState.x + 14;
  if (x + rect.width > window.innerWidth - 8) x = tooltipState.x - 14 - rect.width;
  node.style.left = `${Math.min(Math.max(x, 8), maxX)}px}`;
  node.style.top = `${Math.min(Math.max(tooltipState.y + 14, 8), maxY)}px}`;
  node.style.visibility = '';
}

watch(
  () => [tooltipState.visible, tooltipState.x, tooltipState.y] as const,
  ([v]) => {
    if (v) place();
  },
  { flush: 'post' }, // v-if 节点挂载后再定位（否则 el 为 null 首帧无位置）
);

// 图表数据到达后高度变化 → 重新定位（避免底部越界裁切）
watch(tooltipHistory, () => {
  if (tooltipState.visible) place();
});

const st = computed(() => {
  const s = speedTestState.value;
  return s && s.bind === tooltipState.bind ? s : null;
});
</script>

<template>
  <div
    v-if="tooltipState.visible"
    ref="el"
    class="proxy-tooltip"
    @mouseenter="cancelHide"
    @mouseleave="scheduleHide"
  >
    <div class="proxy-tooltip-header">
      <code>{{ tooltipState.bind }}</code>
      <span class="proxy-tooltip-stats">{{ statsText }}</span>
      <button class="proxy-tooltip-close" title="关闭" @click="hideTooltip()">✕</button>
    </div>
    <div class="proxy-tooltip-charts">
      <div class="proxy-tooltip-section">
        <div class="proxy-tooltip-label">延时趋势</div>
        <div class="h-[120px]"><ChartBox :option="latencyOption" /></div>
      </div>
      <div class="proxy-tooltip-section">
        <div class="proxy-tooltip-label">流量速率</div>
        <div class="h-[120px]"><ChartBox :option="trafficOption" /></div>
      </div>
    </div>
    <div class="proxy-tooltip-speedtest">
      <button
        class="btn btn-ghost btn-sm w-full"
        :disabled="st?.status === 'running'"
        @click="runSpeedTest(tooltipState.bind)"
      >
        {{ st?.status === 'running' ? '测速中...' : '测速 (CF 10s)' }}
      </button>
      <div class="proxy-tooltip-speedtest-result">
        <template v-if="st">
          <span v-if="st.status === 'running'" class="text-[var(--color-t-muted)]">正在测速（最多 10s）...</span>
          <template v-else-if="st.error">
            <span class="text-[var(--color-warning)]">测速失败: {{ st.error }}</span>
          </template>
          <template v-else-if="st.result">
            <div class="flex justify-between">
              <span class="text-[var(--color-t-muted)]">下载</span>
              <span class="font-medium">
                <template v-if="st.result.download?.ok">{{ st.result.download.best_mbps?.toFixed(2) }} Mbps</template>
                <template v-else>失败{{ st.result.download?.err ? ` (${st.result.download.err})` : '' }}</template>
              </span>
            </div>
            <div class="flex justify-between">
              <span class="text-[var(--color-t-muted)]">上传</span>
              <span class="font-medium">
                <template v-if="st.result.upload?.ok">{{ st.result.upload.best_mbps?.toFixed(2) }} Mbps</template>
                <template v-else>失败{{ st.result.upload?.err ? ` (${st.result.upload.err})` : '' }}</template>
              </span>
            </div>
            <div class="text-[var(--color-t-muted)] text-xs mt-1">测速时间: {{ st.time }}</div>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>
