<script setup lang="ts">
/**
 * 流量统计页：两个选项卡——实时流量（此刻仍开着的连接）与历史流量（持久化条目）。
 *
 * - 实时页按固定节拍轮询；页面切后台即停轮询、回前台补一次（与 status store 同口径）。
 * - 历史页进入时拉一次：条目会被持续累加更新（不是时间序列），增量拉取语义不成立，
 *   故只做按需刷新。
 * - 数据源为 TrafficStatsUseCase，见 docs/design/LLD-TrafficStats.md。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { api, errText } from '@/api/client';
import { displayCountryName, fmtBytesSpaced, fmtDateTime } from '@/utils/format';
import type {
  GeoInfo,
  TrafficHistoryEntry,
  TrafficLiveEntry,
  TrafficSummary,
} from '@/types/api';

const POLL_INTERVAL_MS = 5_000;

const TABS = [
  { id: 'live' as const, label: '实时流量' },
  { id: 'history' as const, label: '历史流量' },
];

const tab = ref<'live' | 'history'>('live');
const live = ref<TrafficLiveEntry[]>([]);
const history = ref<TrafficHistoryEntry[]>([]);
const historyTotal = ref(0);
const summary = ref<TrafficSummary | null>(null);
const loadError = ref<string | null>(null);
let timer: number | null = null;

const summaryKpis = computed(() => {
  const s = summary.value;
  if (!s) return [];
  return [
    { testid: 'connections', label: '活跃连接', value: String(s.connections) },
    { testid: 'entries', label: '历史条目', value: String(s.entries) },
    { testid: 'up', label: '累计上行', value: fmtBytesSpaced(s.total.upload) },
    { testid: 'down', label: '累计下行', value: fmtBytesSpaced(s.total.download) },
  ];
});

function stopPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function startPolling(): void {
  if (timer !== null || document.hidden) return;
  timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
}

async function refresh(): Promise<void> {
  try {
    // 摘要两个选项卡都刷（历史页的「历史条目」KPI 同样要跟着条目增长走）
    if (tab.value === 'live') {
      const [liveResp, summaryResp] = await Promise.all([api.trafficLive(), api.trafficSummary()]);
      live.value = liveResp.entries;
      summary.value = summaryResp;
    } else {
      const [hisResp, summaryResp] = await Promise.all([
        api.trafficHistory(),
        api.trafficSummary(),
      ]);
      history.value = hisResp.entries;
      historyTotal.value = hisResp.total_entries;
      summary.value = summaryResp;
    }
    loadError.value = null;
  } catch (err) {
    loadError.value = errText(err);
  }
}

function onVisibility(): void {
  if (document.hidden) {
    stopPolling();
    return;
  }
  void refresh();
  // 只有实时页轮询：历史条目会被持续累加更新，按需刷新即可（与 switchTab 同口径）
  if (tab.value === 'live') startPolling();
}

function switchTab(next: 'live' | 'history'): void {
  if (tab.value === next) return;
  tab.value = next;
  stopPolling();
  void refresh();
  if (next === 'live') startPolling();
}

/** 归属地文本：国家/省/市逐级拼接（与归属列同口径，中国显示为「中国大陆」） */
function geoText(geo: GeoInfo | null): string {
  if (!geo) return '-';
  const parts = [geo.country, geo.province, geo.city]
    .filter((v): v is string => !!v)
    .map(displayCountryName);
  return parts.length ? parts.join(' · ') : '-';
}

function rateText(up: number, down: number): string {
  return `↑ ${fmtBytesSpaced(up)}/s ↓ ${fmtBytesSpaced(down)}/s`;
}

onMounted(() => {
  void refresh();
  startPolling();
  document.addEventListener('visibilitychange', onVisibility);
});

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility);
  stopPolling();
});
</script>

<template>
  <div class="space-y-4">
    <div class="card">
      <div class="card-header">
        <div class="card-title">流量统计</div>
        <div class="flex items-center gap-2">
          <button
            v-for="t in TABS"
            :key="t.id"
            class="btn btn-sm"
            :class="tab === t.id ? 'btn-primary' : 'btn-ghost'"
            :data-testid="`traffic-tab-${t.id}`"
            @click="switchTab(t.id)"
          >
            {{ t.label }}
          </button>
        </div>
      </div>
      <div class="card-body">
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div v-for="k in summaryKpis" :key="k.testid" class="text-center">
            <div class="kpi-value" :data-testid="`traffic-kpi-${k.testid}`">{{ k.value }}</div>
            <div class="kpi-label">{{ k.label }}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div class="card-title">{{ tab === 'live' ? '实时流量' : '历史流量' }}</div>
        <div class="flex items-center gap-3">
          <span class="text-xs text-[var(--color-t-muted)]" data-testid="traffic-scope-hint">
            {{ tab === 'live'
              ? '此刻仍开着的连接（连接关闭即从列表消失）'
              : `共 ${historyTotal} 条，按流量总计降序（超出上限的未列出）` }}
          </span>
          <button class="btn btn-sm btn-ghost" data-testid="traffic-refresh" @click="refresh">刷新</button>
        </div>
      </div>
      <div class="card-body">
        <div
          v-if="loadError"
          class="text-sm text-[var(--color-danger)] mb-3"
          data-testid="traffic-error"
        >
          {{ loadError }}
        </div>

        <template v-if="tab === 'live'">
          <div v-if="live.length" class="table-wrap">
            <table data-testid="traffic-live-table">
              <thead>
                <tr>
                  <th>域名</th>
                  <th>目标 IP</th>
                  <th>归属地</th>
                  <th>代理</th>
                  <th class="text-right">连接数</th>
                  <th class="text-right">速率</th>
                  <th>最近会话</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in live" :key="`${e.bind}|${e.host}`">
                  <td class="text-[var(--color-t-primary)]">{{ e.host }}</td>
                  <td class="font-mono text-xs">{{ e.ip ?? '-' }}</td>
                  <td class="text-[var(--color-t-muted)]">{{ geoText(e.geo) }}</td>
                  <td class="font-mono text-xs">{{ e.bind }}</td>
                  <td class="text-right">{{ e.connections }}</td>
                  <td class="text-right">{{ rateText(e.up_rate, e.down_rate) }}</td>
                  <td class="text-xs text-[var(--color-t-muted)]">
                    {{ e.last_seen ? fmtDateTime(e.last_seen) : '-' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state" data-testid="traffic-live-empty">
            当前没有活跃连接
          </div>
        </template>

        <template v-else>
          <div v-if="history.length" class="table-wrap">
            <table data-testid="traffic-history-table">
              <thead>
                <tr>
                  <th>域名</th>
                  <th>目标 IP</th>
                  <th>归属地</th>
                  <th>代理</th>
                  <th class="text-right">上传</th>
                  <th class="text-right">下载</th>
                  <th class="text-right">总计</th>
                  <th>最后连接</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in history" :key="`${e.bind}|${e.host}|${e.ip ?? ''}`">
                  <td class="text-[var(--color-t-primary)]">{{ e.host }}</td>
                  <td class="font-mono text-xs">{{ e.ip || '-' }}</td>
                  <td class="text-[var(--color-t-muted)]">{{ geoText(e.geo) }}</td>
                  <td class="font-mono text-xs">{{ e.bind }}</td>
                  <td class="text-right">{{ fmtBytesSpaced(e.upload) }}</td>
                  <td class="text-right">{{ fmtBytesSpaced(e.download) }}</td>
                  <td class="text-right">{{ fmtBytesSpaced(e.upload + e.download) }}</td>
                  <td class="text-xs text-[var(--color-t-muted)]">
                    {{ e.last_seen ? fmtDateTime(e.last_seen) : '-' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state" data-testid="traffic-history-empty">
            暂无历史流量
          </div>
        </template>
      </div>
    </div>
  </div>
</template>
