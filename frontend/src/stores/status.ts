/**
 * 状态 store：2s 轮询 /api/status + 按需增量拉取 /api/status/history。
 *
 * 与旧前端行为对齐：
 * - 历史仅仪表盘/队列页需要（historyEnabled 由路由守卫切换），游标=已合并点最大 ts；
 * - 历史前端保留上限 1000 点（后端窗口 1800s / 2s ≈ 900 点，留余量）；
 * - paused 只停轮询（保留最后数据）；首次加载失败弹一次 error toast；
 * - 历史拉取失败不阻断状态刷新（图表滞后一拍，下轮重试）。
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { api, ApiError } from '@/api/client';
import type { StandbyHistoryPoint, StatusHistory, StatusSnapshot, TrafficHistoryPoint } from '@/types/api';
import { useToastStore } from '@/stores/toast';

const REFRESH_INTERVAL = 2000;
export const MAX_HISTORY = 1000;

/** 纯函数：按 ts 顺序追加并裁剪（服务端保证时间序，直接 concat 后截尾）。可单测。 */
export function mergePoints<T extends { ts: number }>(
  existing: T[],
  incoming: T[],
  max: number = MAX_HISTORY,
): T[] {
  if (!incoming.length) return existing;
  const merged = existing.concat(incoming);
  return merged.length > max ? merged.slice(-max) : merged;
}

/** 纯函数：增量游标 = 两序列最后点的最大 ts。可单测。 */
export function historyCursor(standby: { ts: number }[], traffic: { ts: number }[]): number {
  const s = standby.length ? standby[standby.length - 1].ts : 0;
  const t = traffic.length ? traffic[traffic.length - 1].ts : 0;
  return Math.max(s, t);
}

export const useStatusStore = defineStore('status', () => {
  const status = ref<StatusSnapshot | null>(null);
  const standbyHistory = ref<StandbyHistoryPoint[]>([]);
  const trafficHistory = ref<TrafficHistoryPoint[]>([]);
  const connected = ref(false);
  const paused = ref(false);
  const historyEnabled = ref(false);

  let timer: ReturnType<typeof setInterval> | null = null;
  let firstLoadDone = false;

  const lockedProxy = computed(() => status.value?.locked_proxy ?? null);

  function mergeHistory(data: StatusHistory) {
    standbyHistory.value = mergePoints(standbyHistory.value, data.standby_history ?? []);
    trafficHistory.value = mergePoints(trafficHistory.value, data.traffic_history ?? []);
  }

  async function refreshHistory() {
    if (!historyEnabled.value) return;
    try {
      const cursor = historyCursor(standbyHistory.value, trafficHistory.value);
      mergeHistory(await api.statusHistory(cursor || undefined));
    } catch (err) {
      console.warn('history refresh failed', err);
    }
  }

  async function tick() {
    if (paused.value) return;
    try {
      const data = await api.status();
      status.value = data;
      connected.value = true;
      await refreshHistory();
    } catch (err) {
      connected.value = false;
      if (!firstLoadDone) {
        firstLoadDone = true;
        useToastStore().error('连接失败', err instanceof ApiError ? err.message : String(err));
      }
    }
    firstLoadDone = true;
  }

  function start() {
    if (timer) return;
    timer = setInterval(tick, REFRESH_INTERVAL);
    tick();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  function togglePause() {
    paused.value = !paused.value;
    if (!paused.value) tick();
  }

  return {
    status, standbyHistory, trafficHistory, connected, paused, historyEnabled, lockedProxy,
    tick, start, stop, togglePause,
  };
});
