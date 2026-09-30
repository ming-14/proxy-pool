/**
 * 状态 store：固定 2s 轮询 /api/status + 按需增量拉取 /api/status/history。
 *
 * 与旧前端行为对齐：
 * - 历史仅仪表盘/队列页需要（historyEnabled 由路由守卫切换），游标=已合并点最大 ts；
 * - 历史前端按服务端留存时长裁剪（`retention_window` = 后端 METRICS_WINDOW，随响应下发），
 *   不在前端写死点数上限：采样节奏可变（心跳 2s），点数上限与留存时长不等价；
 * - paused 只停轮询（保留最后数据）；首次加载失败弹一次 error toast；
 * - 页面切后台（document.hidden）同样停轮询，切回前台立即补一次——历史走 since 游标
 *   增量，后台期间的点不会丢（与日志面板的可见性口径一致）；
 * - 历史拉取失败不阻断状态刷新（图表滞后一拍，下轮重试）；
 * - 写动作（固定/锁定等）后由页面调用 refreshNow 立即取数：paused 是"停止自动轮询"，
 *   不该压住用户刚触发的操作结果，故 refreshNow 不带 paused 判断。
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { api, errText } from '@/api/client';
import type { StandbyHistoryPoint, StatusHistory, StatusSnapshot, TrafficHistoryPoint } from '@/types/api';
import { useToastStore } from '@/stores/toast';
import { trimWith } from '@/utils/series';

/** /api/status 轮询间隔（固定值，面板无运行时调节项） */
const POLL_INTERVAL_MS = 2000;

/**
 * 纯函数：按 ts 顺序追加，并按留存时长裁掉过期点（服务端保证时间序，直接 concat 后裁头）。
 *
 * 裁剪基准取合并后序列**自身的末点 ts**（服务端时间戳，避免与浏览器时钟偏差叠加）；
 * 按时间而非点数裁剪：点数上限与留存时长不等价——采样节奏一变（心跳 2s，锁定单一
 * 上游时放宽到 30s），同一上限对应的时长就跟着变，快节奏下会裁掉仍在留存期内的点、
 * 让曲线左段凭空消失。可单测。
 */
export function mergePoints<T extends { ts: number }>(
  existing: T[],
  incoming: T[],
  windowS: number,
): T[] {
  if (!incoming.length) return existing;
  const merged = existing.concat(incoming);
  const cutoff = merged[merged.length - 1].ts - windowS;
  return trimWith(merged, cutoff, (p) => p.ts);
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
  /** 防重入：上一轮轮询仍在途时跳过本轮，避免请求堆积（后端僵死时每 2s 叠一个） */
  let inFlight = false;
  /**
   * 请求序号：只接受最新一次请求的响应。
   *
   * 没有它会出现"旧响应覆盖新数据"——写动作后 refreshNow 拿到最新队列，此时一个
   * 更早发出的轮询才姗姗返回，把 status 覆写回它出发时的旧快照，界面于是显示
   * 操作前的状态（toast 说已移动、列表没动）。序号按发出顺序取，晚到的旧响应直接丢弃。
   */
  let requestSeq = 0;

  const lockedProxy = computed(() => status.value?.locked_proxy ?? null);

  function mergeHistory(data: StatusHistory) {
    const windowS = data.retention_window;
    standbyHistory.value = mergePoints(standbyHistory.value, data.standby_history ?? [], windowS);
    trafficHistory.value = mergePoints(trafficHistory.value, data.traffic_history ?? [], windowS);
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

  async function fetchStatus() {
    const seq = ++requestSeq;
    try {
      const data = await api.status();
      if (seq !== requestSeq) return; // 已有更新的请求发出，丢弃这份过期快照
      status.value = data;
      connected.value = true;
      await refreshHistory();
    } catch (err) {
      if (seq !== requestSeq) return;
      connected.value = false;
      if (!firstLoadDone) {
        firstLoadDone = true;
        useToastStore().error('连接失败', errText(err));
      }
    }
    firstLoadDone = true;
  }

  /**
   * 定时轮询走这里：上一轮仍在途时跳过本轮。
   *
   * 只为挡住"请求比轮询间隔还慢"造成的无界堆积（后端僵死时每 2s 叠一个请求）。
   * 用户触发的补拉（切回前台）与写动作后的 refreshNow 不走这个门——它们要的是
   * "立刻取一次"，被在途请求挡掉会让行为与预期不符。
   */
  async function tick() {
    // 页面切后台不发请求（与日志面板同一口径）；切回前台由 visibilitychange 立即补一次，
    // 历史走 since 游标增量，期间的点不会丢
    if (paused.value || document.hidden) return;
    if (inFlight) return;
    inFlight = true;
    try {
      await fetchStatus();
    } finally {
      inFlight = false;
    }
  }

  /**
   * 写动作后的即时刷新：忽略暂停（用户显式操作的结果必须立刻反映，不等下一次轮询），
   * 也不受在途轮询影响——调用方依赖它返回后读到的 status 就是最新队列，
   * 被挡掉会让 toast 与列表对不上。
   */
  async function refreshNow() {
    await fetchStatus();
  }

  function onVisibilityChange() {
    // 切回前台立即补一次：这是用户可见的"页面活了"，不走 tick 的防重入门
    if (!document.hidden) void fetchStatus();
  }

  function start() {
    if (timer) return;
    document.addEventListener('visibilitychange', onVisibilityChange);
    timer = setInterval(tick, POLL_INTERVAL_MS);
    void fetchStatus(); // 首帧立即取数
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    document.removeEventListener('visibilitychange', onVisibilityChange);
  }

  function togglePause() {
    paused.value = !paused.value;
    // 恢复轮询：立即取一次（仍受页面可见性约束——后台标签页不该被唤醒发请求）
    if (!paused.value && !document.hidden) void fetchStatus();
  }

  return {
    status, standbyHistory, trafficHistory, connected, paused, historyEnabled, lockedProxy,
    tick, refreshNow, start, stop, togglePause,
  };
});
