<script setup lang="ts">
/**
 * 仪表盘：KPI 卡片、队列延迟/健康率趋势、数据流量图+速率 KPI、IP 分布地图。
 * 数据全部来自 statusStore（2s 轮询 + 历史增量），图表 option 为纯 computed——
 * 旧版增量渲染状态机与 innerHTML 重建补丁全部移除。
 */
import { computed } from 'vue';
import ChartBox from '@/components/charts/ChartBox.vue';
import GeoMap from '@/components/charts/GeoMap.vue';
import { lineChartOption } from '@/components/charts/chartOptions';
import { useLineTheme } from '@/components/charts/useLineTheme';
import { useStatusStore } from '@/stores/status';
import { fmtBytesSpaced, fmtTime } from '@/utils/format';
import type { TrafficHistoryPoint } from '@/types/api';

const store = useStatusStore();
const theme = useLineTheme();

const status = computed(() => store.status);
const lockedProxy = computed(() => status.value?.locked_proxy ?? null);

// ---------------- KPI ----------------
const kpis = computed(() => {
  const s = status.value;
  if (!s) return [];
  const groups = Object.values(s.groups);
  const trusted = groups.find((g) => g.kind === 'trusted');
  const blacklist = groups.find((g) => g.kind === 'blacklist');
  return [
    { testid: 'available', label: '可用代理', value: String(s.available_count ?? '-'), sub: `${groups.length} 个分组`, color: 'text-[var(--color-primary-600)]' },
    s.locked_proxy
      ? { testid: 'standby', label: '代理服务队列', value: '固定代理', sub: s.locked_proxy, color: 'text-[var(--color-warning)]' }
      : { testid: 'standby', label: '代理服务队列', value: `${s.standby.length} / ${s.capacity ?? '-'}`, sub: 'pproxy 上游', color: 'text-[var(--color-success)]' },
    { testid: 'trusted', label: trusted?.label ?? '信任池', value: String(trusted?.count ?? '-'), sub: '请求成功晋级', color: 'text-[var(--color-warning)]' },
    { testid: 'blacklist', label: blacklist?.label ?? '黑名单', value: String(blacklist?.count ?? '-'), sub: '连续失败淘汰', color: 'text-[var(--color-danger)]' },
  ];
});

// ---------------- 趋势图 ----------------
const latencyOption = computed(() => {
  const h = store.standbyHistory;
  return lineChartOption(
    {
      labels: h.map((p) => fmtTime(p.ts)),
      series: [
        { name: '平均延迟', data: h.map((p) => p.avg), color: theme.value.primary, fillAlpha: '20' },
        { name: '最小延迟', data: h.map((p) => p.min), color: theme.value.success },
        { name: '最大延迟', data: h.map((p) => p.max), color: theme.value.warning },
      ],
      axes: [{ type: 'log' }],
    },
    theme.value,
  );
});

const healthOption = computed(() => {
  const h = store.standbyHistory;
  return lineChartOption(
    {
      labels: h.map((p) => fmtTime(p.ts)),
      series: [
        // 锁定期间采集的点无健康意义 → null 断线（与旧版一致）
        { name: '健康率 (%)', data: h.map((p) => (p.locked ? null : p.health_rate)), color: theme.value.success, fillAlpha: '20' },
        { name: '队列长度', data: h.map((p) => (p.locked ? null : p.queue_length)), color: theme.value.primary, fillAlpha: '15', yAxisIndex: 1 },
      ],
      axes: [
        { type: 'value', min: 0, max: 100, name: '健康率 %' },
        { type: 'value', position: 'right', name: '数量' },
      ],
    },
    theme.value,
  );
});

/** 累计字节序列 → 相邻差分速率（与旧 renderTraffic 语义一致：首点无差分被跳过） */
function trafficRates(th: TrafficHistoryPoint[]) {
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
  return { labels, up, down };
}

const trafficOption = computed(() => {
  const { labels, up, down } = trafficRates(store.trafficHistory);
  const sh = store.standbyHistory;
  // 平均延迟取 traffic 序列同索引起点偏移 1（差分点 i 对应 standby[i]）
  const avg = sh.slice(1, 1 + labels.length).map((p) => p.avg);
  return lineChartOption(
    {
      labels,
      series: [
        { name: '上行', data: up, color: theme.value.primary, fillAlpha: '20' },
        { name: '下行', data: down, color: theme.value.success, fillAlpha: '20' },
        { name: '平均延迟', data: avg, color: theme.value.warning, yAxisIndex: 1 },
      ],
      axes: [
        { type: 'log', name: '字节/s' },
        { type: 'log', position: 'right', name: '延迟 s' },
      ],
    },
    theme.value,
  );
});

// ---------------- 流量 KPI ----------------
const trafficKpi = computed(() => {
  const th = store.trafficHistory;
  let upRate = 0;
  let downRate = 0;
  if (th.length >= 2) {
    const a = th[th.length - 2];
    const b = th[th.length - 1];
    const dt = b.ts - a.ts;
    if (dt > 0) {
      upRate = (b.total_upload - a.total_upload) / dt;
      downRate = (b.total_download - a.total_download) / dt;
    }
  }
  const total = status.value?.traffic_total ?? { upload: 0, download: 0 };
  const sh = store.standbyHistory;
  const avgLatency = sh.length ? sh[sh.length - 1].avg : null;
  return [
    { label: '实时上行', value: `${fmtBytesSpaced(upRate)}/s`, color: 'text-[var(--color-primary-600)]' },
    { label: '实时下行', value: `${fmtBytesSpaced(downRate)}/s`, color: 'text-[var(--color-success)]' },
    { label: '累计流量', value: fmtBytesSpaced((total.upload || 0) + (total.download || 0)), color: '' },
    { label: '平均延迟', value: avgLatency != null ? `${avgLatency.toFixed(3)}s` : '-', color: '' },
  ];
});
</script>

<template>
  <div class="space-y-6">
  <!-- 首次数据到达前：骨架屏（与旧版一致） -->
  <template v-if="!status">
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div v-for="i in 4" :key="i" class="skeleton skeleton-card" />
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <div class="card p-4"><div class="skeleton skeleton-title" /><div class="h-56 skeleton" /></div>
      <div class="card p-4"><div class="skeleton skeleton-title" /><div class="h-56 skeleton" /></div>
    </div>
  </template>

  <template v-else>
    <!-- KPI -->
    <div data-testid="kpi-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div v-for="k in kpis" :key="k.label" class="card card-body card-hover" :data-testid="`kpi-${k.testid}`">
        <div class="kpi-label">{{ k.label }}</div>
        <div class="kpi-value" :class="k.color">{{ k.value }}</div>
        <div class="kpi-delta text-[var(--color-t-muted)]">{{ k.sub }}</div>
      </div>
    </div>

    <!-- 延迟/健康率趋势：锁定时隐藏健康率卡，延迟卡占满整行 -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <div data-testid="latency-card" class="card p-4" :class="{ 'lg:col-span-2': lockedProxy }">
        <div class="card-title mb-4">代理服务队列延迟趋势</div>
        <div data-testid="latency-chart" class="h-56"><ChartBox :option="latencyOption" /></div>
      </div>
      <div v-if="!lockedProxy" data-testid="health-card" class="card p-4">
        <div class="card-title mb-4">代理服务队列健康率趋势</div>
        <div data-testid="health-chart" class="h-56"><ChartBox :option="healthOption" /></div>
      </div>
    </div>

    <!-- 数据流量 -->
    <div data-testid="traffic-card" class="card p-4">
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="lg:col-span-2">
          <div class="card-title mb-4">数据流量</div>
          <div data-testid="traffic-chart" class="h-56"><ChartBox :option="trafficOption" /></div>
        </div>
        <div data-testid="traffic-kpi" class="space-y-3 text-sm">
          <div v-for="t in trafficKpi" :key="t.label" class="flex justify-between">
            <span class="text-[var(--color-t-muted)]">{{ t.label }}</span>
            <span class="font-medium" :class="t.color">{{ t.value }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- IP 分布地图（geoip.enabled=false 时后端不下发 geo_enabled，区块隐藏） -->
    <GeoMap v-if="status.geo_enabled" />
  </template>
  </div>
</template>
