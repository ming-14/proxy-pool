<script setup lang="ts">
/**
 * 代理服务队列页：每个代理一块 = 摘要行 + 行下质量带。
 *
 * 摘要行：序号 / 代理（地址 + 协议·归属地·入队时长 + 纯洁度 tag）/ 延迟 / 连续失败 /
 *         操作（纯洁 · 测速 · 固定 · 锁定）。
 * 质量带：行下常显的行内趋势图（延时趋势 / 流量速率）与质量指标（纯洁度 / 测速结果）——
 *         不做逐项显示开关（进页面即全部可见），只有「收起/展开图表」一个密度开关：
 *         收起后回到单行密度便于纵览全队列，展开与否跨会话记住。
 *         没有采样的区块显示"暂无采样/未检查/未测速"占位。
 *
 * 数据来源：
 * - 状态（延迟/失败/固定/锁定）走 store 的 /api/status 2s 轮询；
 * - 逐代理序列走 /api/standby/series 增量（同一 2s 节拍驱动，见 useStandbySeries）；
 * - 归属地 5 分钟节流拉取（useStandbyGeo）；
 * - 纯洁度与测速是手动触发的重操作，结果就地展示（不做后台轮询）。
 *
 * 顺序冻结：选中/拖拽/悬浮期间按旧顺序渲染——值随轮询更新、行不跑位（否则 2s 一轮的
 * 延迟排序会把正在读的行从光标下挪走）；固定/锁定等写动作随后 refreshNow 穿透冻结回正。
 *
 * 锁定单一上游时锁定代理作为列表首行（行内「已锁定」标记，操作列只剩纯洁/测速/解锁），
 * 其余行灰显且不可交互——不单独开锁定卡片。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { useToastStore } from '@/stores/toast';
import { useRowSelection } from '@/composables/useRowSelection';
import {
  checkPurity,
  purityEntryOf,
  purityScoreClass,
  usageTypeShort,
} from '@/composables/usePurity';
import { runSpeedTest, speedTestEntryOf } from '@/composables/useSpeedTest';
import { GEO_REFRESH_MS, geoText, refreshStandbyGeo, standbyGeo } from '@/composables/useStandbyGeo';
import { refreshStandbySeries, seriesBuffers } from '@/composables/useStandbySeries';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import Icon from '@/components/ui/Icon.vue';
import PurityTags from '@/components/PurityTags.vue';
import Sparkline from '@/components/Sparkline.vue';
import {
  bindOf,
  fmtAge,
  fmtBytes,
  fmtLatency,
  speedAllFailed,
  speedSummary,
  speedSummaryTitle,
} from '@/utils/format';
import { mergeKeepOrder } from '@/utils/merge';
import { seriesTimeWindow, sparkStats, toRates, type NumericPoint } from '@/utils/series';
import type { MenuItem } from '@/types/ui';
import type { StandbyEntry } from '@/types/api';

const store = useStatusStore();
const toast = useToastStore();
const GEO_TIP = `归属地每 ${Math.round(GEO_REFRESH_MS / 60000)} 分钟刷新`;

const status = computed(() => store.status);
const standbyList = computed(() => status.value?.standby ?? []);
/** 是否已锁定单一上游（锁定行的其余队列行灰显且不可交互）——以后端 locked_proxy 为准，
 *  与列表内的 locked 行标记同源（api.py 由同一次 is_locked() 判定下发） */
const hasLock = computed(() => store.lockedProxy !== null);
const groups = computed(() => status.value?.groups ?? {});
const pending = computed(() => status.value?.pending_retries ?? []);
const geoEnabled = computed(() => status.value?.geo_enabled === true);

const tableWrapRef = ref<HTMLElement | null>(null);
/** 质量带展开态：收起后每代理只占一行，便于纵览全队列（跨会话记住选择） */
const STRIP_KEY = 'proxy-pool-standby-strip';
const stripExpanded = ref(localStorage.getItem(STRIP_KEY) !== '0');
watch(stripExpanded, (v) => localStorage.setItem(STRIP_KEY, v ? '1' : '0'));
/** 鼠标所在行：停在表内时冻结顺序（值照常更新，行不跑位） */
const hoveredProxy = ref<string | null>(null);
const menu = ref<{ items: MenuItem[]; x: number; y: number } | null>(null);
const contextProxy = ref('');

// ---------------- 行选择 ----------------
function indexAtY(clientY: number): number {
  const rows = displayStandby.value;
  if (!rows.length) return -1;
  const tbody = tableWrapRef.value?.querySelector('tbody');
  const trs = tbody ? Array.from(tbody.children) : [];
  if (!trs.length) return -1;
  const first = trs[0].getBoundingClientRect();
  const last = trs[trs.length - 1].getBoundingClientRect();
  if (clientY < first.top) return 0;
  if (clientY > last.bottom) return trs.length - 1;
  for (let i = 0; i < trs.length; i++) {
    const r = trs[i].getBoundingClientRect();
    if (clientY >= r.top && clientY <= r.bottom) return i;
  }
  return 0;
}

const selection = useRowSelection<StandbyEntry>({
  container: tableWrapRef,
  scrollEl: () => document.getElementById('main-content'),
  getRows: () => displayStandby.value,
  proxyOf: (r) => r.proxy,
  indexAtY,
  // 锁定期间队列行不参与框选/多选（等价旧版"整卡禁用"；锁定行的按钮仍可用）。
  // 必须整体禁用：只挡鼠标会被键盘 Ctrl+A 绕过（它走 document keydown，不经过
  // isExcludedTarget），于是灰显行被全选、叠上选中高亮并一直冻结列表顺序
  isSelectionDisabled: () => hasLock.value,
  isExcludedTarget: (e) => !!(e.target as HTMLElement).closest('code, button'),
});

// ---------------- 顺序冻结（选中/拖拽/悬浮期间行不跑位） ----------------
const displayStandby = ref<StandbyEntry[]>([]);
watch(
  standbyList,
  (list) => {
    const freeze = selection.isDragging() || selection.count() > 0 || !!hoveredProxy.value;
    displayStandby.value = freeze ? mergeKeepOrder(displayStandby.value, list, (p) => p.proxy) : [...list];
  },
  { immediate: true },
);

// 锁定后队列行不可交互：清空残留选中——否则选中态会继续冻结列表顺序，
// 并在灰显行上叠一层选中高亮（既不可操作又看着像被选中）。
// 此后新产生的选中由 useRowSelection 的 isSelectionDisabled 挡住（鼠标与键盘一致）
watch(hasLock, (locked) => {
  if (locked) selection.clear();
});

// ---------------- 逐代理序列 / 归属地（随 2s 状态节拍刷新） ----------------
/** 状态对象每轮轮询都是新引用 → 以此节拍驱动序列增量与归属地（内部节流）刷新；
 *  immediate 让进入页面时立刻取一次，不必等下一个 2s 轮询 */
watch(
  status,
  (snap) => {
    if (!snap) return;
    void refreshStandbySeries(displayStandby.value.map((p) => bindOf(p.proxy)));
    void refreshStandbyGeo(geoEnabled.value);
  },
  { immediate: true },
);

// ---------------- 队列动作 ----------------
/**
 * 写动作成功后立即按最新顺序渲染。
 * 顺序冻结是给后台 2s 轮询用的（选中/拖拽/悬浮期间不让行在光标下跑位），
 * 但固定/解固/锁定/解锁是用户显式触发的重排，必须立刻可见——否则 toast 说
 * "代理已移到队列最前"而列表原地不动，且选中态会让冻结一直不解除、顺序永不回正。
 */
async function refreshQueueOrder() {
  await store.refreshNow();
  displayStandby.value = [...(store.status?.standby ?? [])];
}

async function doPin(proxy: string) {
  try {
    await api.pin(proxy);
    await refreshQueueOrder();
    toast.success('已固定', '代理已移到队列最前');
  } catch (e) {
    toast.error('固定失败', e instanceof Error ? e.message : String(e));
  }
}

async function doUnpin(proxy: string) {
  try {
    await api.unpin(proxy);
    await refreshQueueOrder();
    toast.success('已解固', '代理回到延迟排序');
  } catch (e) {
    toast.error('解固失败', e instanceof Error ? e.message : String(e));
  }
}

async function doLock(proxy: string) {
  try {
    await api.lock(proxy);
    await refreshQueueOrder();
    toast.success('已锁定', `只使用 ${proxy}`);
  } catch (e) {
    toast.error('锁定失败', e instanceof Error ? e.message : String(e));
  }
}

async function doUnlock() {
  try {
    await api.unlock();
    await refreshQueueOrder();
    toast.success('已解锁', '恢复代理服务队列');
  } catch (e) {
    toast.error('解锁失败', e instanceof Error ? e.message : String(e));
  }
}

// ---------------- 每块的质量视图（一次算好，模板直接取） ----------------
interface RowView {
  lat: NumericPoint[];
  latNote: string;
  traffic: { pts: NumericPoint[]; color: string }[];
  trafficNote: string;
}

/** 行内流量图左侧数据值：字节/秒（与行内「累计」同一套字节格式化） */
const fmtRate = (v: number) => `${fmtBytes(v)}/s`;

/**
 * 行内趋势图的共享时间窗口（全部行、同一行的两条曲线共用）。
 * 每行自己归一化会让"刚入队只有两点"与"跑了半小时"的行长得一样、采样已停止的行
 * 也贴着右边缘——共享窗口后右端留白就是陈旧，左端留白就是新入队。
 */
const sparkWindow = computed<[number, number]>(() => {
  const buffers = [];
  for (const row of displayStandby.value) {
    const buf = seriesBuffers.get(bindOf(row.proxy));
    if (buf) buffers.push(buf);
  }
  return seriesTimeWindow(buffers, Date.now() / 1000);
});

const EMPTY_VIEW: RowView = {
  lat: [],
  latNote: '窗口内无探测',
  traffic: [],
  trafficNote: '窗口内无流量',
};

const viewByBind = computed(() => {
  const out = new Map<string, RowView>();
  for (const row of displayStandby.value) {
    const bind = bindOf(row.proxy);
    const buf = seriesBuffers.get(bind);
    const lat = buf?.lat ?? [];
    const stats = sparkStats(lat);
    const lastUp = buf?.up.length ? buf.up[buf.up.length - 1][1] : 0;
    const lastDown = buf?.down.length ? buf.down[buf.down.length - 1][1] : 0;
    const hasTraffic = !!buf?.up.length || !!buf?.down.length;
    out.set(bind, {
      lat,
      latNote: stats.rate === null ? '窗口内无探测' : `成功率 ${stats.rate}%（${stats.total} 次探测）`,
      traffic: [
        { pts: toRates(buf?.up ?? []), color: 'var(--color-primary-600)' },   // 上行
        { pts: toRates(buf?.down ?? []), color: 'var(--color-success)' },     // 下行
      ],
      trafficNote: hasTraffic ? `累计 ↑${fmtBytes(lastUp)} ↓${fmtBytes(lastDown)}` : '窗口内无流量',
    });
  }
  return out;
});

function viewOf(row: StandbyEntry): RowView {
  return viewByBind.value.get(bindOf(row.proxy)) ?? EMPTY_VIEW;
}

function geoTextOf(row: StandbyEntry): string | null {
  return geoText(standbyGeo.get(bindOf(row.proxy)));
}

/** 入队时长（锁定行的 joined_at 是锁定时刻，语义不同故不展示） */
function joinedTextOf(row: StandbyEntry): string | null {
  if (row.locked || row.joined_at === null) return null;
  return `入队 ${fmtAge(Date.now() / 1000 - row.joined_at)}`;
}

/** 行内纯洁度/测速状态（薄包装：本地 Map → 后端状态 → 缓存结果，模板调用简洁） */
function rowPurity(row: StandbyEntry): ReturnType<typeof purityEntryOf> {
  return purityEntryOf(bindOf(row.proxy), row.purity_state, row.purity);
}

function rowSpeedTest(row: StandbyEntry): ReturnType<typeof speedTestEntryOf> {
  return speedTestEntryOf(bindOf(row.proxy), row.speedtest);
}

// ---------------- 右键菜单 ----------------
/** 打开菜单的公共入口（桌面右键 / 触屏长按共用） */
function openMenuAt(tr: HTMLElement, x: number, y: number) {
  const proxy = tr.dataset.proxy;
  if (!proxy) return;
  if (!selection.has(proxy)) selection.selectOne(proxy);
  contextProxy.value = proxy;
  menu.value = { items: buildMenuItems(), x, y };
}

function onContextmenu(e: MouseEvent) {
  // 触屏长按：菜单由下方 touch 计时器弹出（iOS 长按不派发 contextmenu），这里只抑制
  // 系统原生菜单，并兜底直接弹（与计时器幂等：同一份 items/x/y 覆盖，不会重复渲染）。
  // Chrome 触屏长按派发的 contextmenu 实为 PointerEvent（pointerType='touch'）；
  // 桌面鼠标派发普通 MouseEvent，断言后 pointerType 为 undefined → 走下方桌面分支
  const pointerType = (e as PointerEvent).pointerType;
  if (pointerType === 'touch' || pointerType === 'pen') {
    e.preventDefault();
    longPressFired = true; // 与计时器路径一致：抬手抑制合成 click，否则菜单刚开就被关掉
    const tr = (e.target as HTMLElement).closest('tbody tr');
    if (tr) openMenuAt(tr as HTMLElement, e.clientX, e.clientY);
    return;
  }
  // 桌面右键：有文本选区时让出原生菜单（复制）。触屏不适用——长按本身就是选区手势，
  // 若也走这个让路，菜单永远弹不出来（且触屏已用 CSS 禁选区，见 components.css）
  if (window.getSelection()?.toString()) return;
  const tr = (e.target as HTMLElement).closest('tbody tr');
  if (!tr) return;
  e.preventDefault();
  openMenuAt(tr as HTMLElement, e.clientX, e.clientY);
}

// ---------------- 触屏长按（弹右键菜单） ----------------
// iOS Safari 长按不派发 contextmenu 事件，必须 touch 计时自检；Android 的 contextmenu
// 交给 onContextmenu 的 touch 分支抑制原生菜单，菜单本体统一由这里弹出
const LONG_PRESS_MS = 500;
const TOUCH_SLOP = 10; // 长按判定允许的手指位移，超过视为滚动/滑动（取消计时）
let touchTimer: ReturnType<typeof setTimeout> | null = null;
let touchStart: { x: number; y: number } | null = null;
let longPressFired = false;

function clearTouchTimer() {
  if (touchTimer !== null) {
    clearTimeout(touchTimer);
    touchTimer = null;
  }
}

function onTableTouchStart(e: TouchEvent) {
  const t = e.touches[0];
  if (!t) return;
  clearTouchTimer();
  touchStart = { x: t.clientX, y: t.clientY };
  longPressFired = false;
  touchTimer = setTimeout(() => {
    touchTimer = null;
    if (!touchStart) return;
    // 按住期间手指可能轻微漂移，用按下点定位行（elementFromPoint 要求目标可见，
    // 按下点此刻仍在原行上，取的是按住那一刻的元素）
    const el = document.elementFromPoint(touchStart.x, touchStart.y);
    const tr = el ? ((el as HTMLElement).closest('tbody tr') as HTMLElement | null) : null;
    if (!tr) return;
    longPressFired = true;
    openMenuAt(tr, touchStart.x, touchStart.y);
  }, LONG_PRESS_MS);
}

function onTableTouchMove(e: TouchEvent) {
  const t = e.touches[0];
  if (!touchStart || !t) return;
  if (Math.hypot(t.clientX - touchStart.x, t.clientY - touchStart.y) > TOUCH_SLOP) clearTouchTimer();
}

function onTableTouchEnd(e: TouchEvent) {
  clearTouchTimer();
  touchStart = null;
  if (longPressFired) {
    longPressFired = false;
    // 抑制长按抬手后的合成 click：否则 ContextMenu 的文档 click 监听会把刚开的菜单立刻关掉
    e.preventDefault();
  }
}

function onTableTouchCancel() {
  clearTouchTimer();
  touchStart = null;
  longPressFired = false;
}

function buildMenuItems(): MenuItem[] {
  const children = Object.entries(groups.value)
    .filter(([, g]) => g.kind !== 'standby' && g.kind !== 'blacklist')
    .map(([name, g]) => ({ label: g.label, action: 'move', group: name, meta: String(g.count) }));
  const items: MenuItem[] = [
    { label: '移出', children },
    { label: '拉黑', action: 'blacklist' },
  ];
  // 复制/测速只对单行有意义（复制的是菜单所在行），多选时隐藏
  if (selection.count() <= 1) {
    items.unshift({ label: '复制', action: 'copy' });
    items.push({ label: '测速', action: 'speedtest' });
  }
  return items;
}

/** 复制代理地址：clipboard API 需要安全上下文（https/localhost），局域网 http 访问回退 execCommand */
async function copyProxy(proxy: string) {
  try {
    await navigator.clipboard.writeText(proxy);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = proxy;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast.success('已复制', proxy);
}

async function onMenuAction(item: MenuItem) {
  menu.value = null;
  const proxies = selection.getSelectedArray();
  const list = proxies.length ? proxies : [contextProxy.value];
  if (item.action === 'copy') {
    void copyProxy(contextProxy.value);
    return;
  }
  if (item.action === 'speedtest') {
    void runSpeedTest(bindOf(contextProxy.value)); // 测速不改队列，无需回正顺序
    return;
  }
  if (item.action === 'move' && item.group) {
    const target = item.group;
    try {
      const d = await api.moveProxy(list, target);
      toast.success('已移出', `已将 ${d.count} 个代理移到 ${groups.value[target]?.label ?? target}`);
      selection.clear();
      await refreshQueueOrder();
    } catch (e) {
      toast.error('移出失败', e instanceof Error ? e.message : String(e));
    }
    return;
  }
  if (item.action === 'blacklist') {
    try {
      const d = await api.blacklist(list);
      toast.success('已拉黑', `已将 ${d.count} 个代理加入黑名单`);
      selection.clear();
      await refreshQueueOrder();
    } catch (e) {
      toast.error('拉黑失败', e instanceof Error ? e.message : String(e));
    }
  }
}

// ---------------- 悬浮判定（冻结顺序用） ----------------
// 用 pointer 事件替代 mouseover/mouseleave：触屏每次 tap 只合成一次 mouseover、抬手
// 不派发 mouseleave → hoveredProxy 永远不归 null → 顺序永久冻结。pointer 事件在抬手
// 时派发 pointerleave（隐式捕获释放），pointerup 对触屏再兜底清一次（跨浏览器保险）
function onTablePointerover(e: PointerEvent) {
  const tr = (e.target as HTMLElement).closest('tbody tr');
  hoveredProxy.value = tr ? ((tr as HTMLElement).dataset.proxy || null) : null;
}

function onTablePointerleave() {
  hoveredProxy.value = null;
}

function onTablePointerup(e: PointerEvent) {
  if (e.pointerType === 'touch' || e.pointerType === 'pen') hoveredProxy.value = null;
}

/** 主区域滚动：收回右键菜单（菜单按视口固定定位，不跟随滚动） */
function onMainScroll() {
  menu.value = null;
}

onMounted(() => {
  document.getElementById('main-content')?.addEventListener('scroll', onMainScroll, { passive: true });
});

onBeforeUnmount(() => {
  document.getElementById('main-content')?.removeEventListener('scroll', onMainScroll);
  clearTouchTimer();
  hoveredProxy.value = null; // 离开页面解除冻结，避免串页后仍冻住顺序
});
</script>

<template>
  <div class="space-y-5">
    <!-- 队列卡片：每块 = 摘要行 + 行下质量带（锁定期间锁定行置顶高亮，其余行灰显） -->
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">代理服务队列</div>
          <div class="card-subtitle">
            {{ hasLock ? '已锁定单一上游代理' : 'pproxy 队列' }}
          </div>
        </div>
        <button
          class="btn btn-ghost btn-sm"
          data-testid="btn-toggle-strip"
          :title="stripExpanded ? '收起质量带，回到单行密度' : '展开质量带，查看每行趋势图'"
          @click="stripExpanded = !stripExpanded"
        >{{ stripExpanded ? '收起图表' : '展开图表' }}</button>
      </div>
      <div
        id="sb-table"
        ref="tableWrapRef"
        data-testid="sb-table"
        class="table-wrap sb-table border-0 rounded-t-none"
        @pointerover="onTablePointerover"
        @pointerleave="onTablePointerleave"
        @pointerup="onTablePointerup"
        @pointercancel="onTablePointerup"
        @touchstart="onTableTouchStart"
        @touchmove="onTableTouchMove"
        @touchend="onTableTouchEnd"
        @touchcancel="onTableTouchCancel"
        @contextmenu="onContextmenu"
      >
        <div v-if="!displayStandby.length" class="empty-state">
          <Icon name="activity" />
          <div>代理服务队列为空</div>
          <div class="text-xs mt-1">等待 serve 主循环自动补入</div>
        </div>
        <table v-else>
          <thead>
            <tr><th>#</th><th>代理</th><th>延迟</th><th>连续失败</th><th class="text-right">操作</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="(p, i) in displayStandby"
              :key="p.proxy"
              :data-idx="i"
              :data-proxy="p.proxy"
              :data-locked="p.locked ? '1' : null"
              :class="{
                selected: selection.has(p.proxy),
                'row-locked': p.locked,
                'row-dimmed': hasLock && !p.locked,
              }"
            >
              <td colspan="5" class="pblock">
                <div class="prow">
                  <span class="prow-idx">{{ i + 1 }}</span>
                  <div class="prow-proxy">
                    <div>
                      <code>{{ p.proxy }}</code>
                      <span
                        v-if="p.locked"
                        class="badge badge-warning ml-2"
                        title="无论心跳是否成功，只使用此代理"
                      >已锁定</span>
                      <PurityTags :data="rowPurity(p)" />
                    </div>
                    <div class="proxy-meta">
                      <span v-if="geoEnabled" :title="GEO_TIP">{{ geoTextOf(p) ?? '归属未知' }}</span>
                      <span v-if="joinedTextOf(p)">{{ joinedTextOf(p) }}</span>
                    </div>
                  </div>
                  <div>{{ fmtLatency(p.latency) }}</div>
                  <div>{{ p.consecutive_fails ?? 0 }}</div>
                  <div class="prow-actions">
                    <button
                      class="btn btn-ghost btn-sm"
                      data-action="purity"
                      :disabled="rowPurity(p)?.status === 'checking'"
                      title="查询 IP 滥用置信度与类型（结果写入 30 天缓存）"
                      @click="checkPurity(p.proxy)"
                    >{{ rowPurity(p)?.status === 'checking' ? '检查中' : '检查纯净度' }}</button>
                    <button
                      class="btn btn-ghost btn-sm"
                      data-action="speedtest"
                      :disabled="rowSpeedTest(p)?.status === 'running'"
                      title="上传/下载测速"
                      @click="runSpeedTest(bindOf(p.proxy))"
                    >{{ rowSpeedTest(p)?.status === 'running' ? '测速中' : '测速' }}</button>
                    <!-- 锁定行只能解锁（换锁需先解锁）；其余行固定/解固 + 锁定 -->
                    <button
                      v-if="p.locked"
                      class="btn btn-warning btn-sm"
                      data-testid="unlock-btn"
                      @click="doUnlock"
                    >解锁</button>
                    <template v-else>
                      <button
                        v-if="p.pinned"
                        class="btn btn-ghost btn-sm"
                        data-action="unpin"
                        @click="doUnpin(p.proxy)"
                      >解固</button>
                      <button v-else class="btn btn-warning btn-sm" data-action="pin" @click="doPin(p.proxy)">固定</button>
                      <button class="btn btn-danger btn-sm" data-action="lock" @click="doLock(p.proxy)">锁定</button>
                    </template>
                  </div>
                </div>

                <!-- 质量带：行内趋势图 + 质量指标（常显，仅整体收起/展开） -->
                <div v-if="stripExpanded" class="pstrip">
                  <div class="pstrip-chart" data-testid="strip-latency">
                    <div class="pstrip-label">
                      <span>延时趋势</span>
                      <span class="pstrip-note">{{ viewOf(p).latNote }}</span>
                    </div>
                    <Sparkline
                      :series="[{ pts: viewOf(p).lat, color: 'var(--color-primary-600)' }]"
                      :window="sparkWindow"
                      :format-value="fmtLatency"
                      log
                    />
                  </div>
                  <div class="pstrip-chart" data-testid="strip-traffic">
                    <div class="pstrip-label">
                      <span>流量速率</span>
                      <span class="pstrip-note">{{ viewOf(p).trafficNote }}</span>
                    </div>
                    <Sparkline
                      :series="viewOf(p).traffic"
                      :window="sparkWindow"
                      :format-value="fmtRate"
                      empty-text="无速率"
                    />
                  </div>
                  <div class="pstrip-side" data-testid="strip-side">
                    <template v-if="rowPurity(p)?.status === 'checking'">
                      <span class="badge badge-muted" data-testid="strip-purity">纯洁度检查中…</span>
                    </template>
                    <span v-else-if="rowPurity(p)?.error" class="badge badge-danger" data-testid="strip-purity">纯洁度检查失败</span>
                    <template v-else-if="rowPurity(p)?.abuse_confidence_score != null">
                      <span
                        class="badge"
                        :class="purityScoreClass(rowPurity(p)!.abuse_confidence_score!)"
                        data-testid="strip-purity"
                        title="滥用置信度（0-100，越低越干净）"
                      >滥用 {{ rowPurity(p)!.abuse_confidence_score }}</span>
                      <span class="badge badge-info" :title="rowPurity(p)!.usage_type || ''">
                        {{ usageTypeShort(rowPurity(p)!.usage_type || '') }}
                      </span>
                    </template>
                    <span v-else class="badge badge-muted" data-testid="strip-purity">纯洁度未检查</span>
                    <span v-if="rowSpeedTest(p)?.status === 'running'" class="badge badge-muted" data-testid="strip-speedtest">测速中…</span>
                    <span v-else-if="rowSpeedTest(p)?.error" class="badge badge-danger" data-testid="strip-speedtest">
                      测速失败：{{ rowSpeedTest(p)!.error }}
                    </span>
                    <span
                      v-else-if="rowSpeedTest(p)?.result"
                      class="badge"
                      :class="speedAllFailed(rowSpeedTest(p)!.result!) ? 'badge-danger' : 'badge-info'"
                      :title="speedSummaryTitle(rowSpeedTest(p)!.result!)"
                      data-testid="strip-speedtest"
                    >{{ speedSummary(rowSpeedTest(p)!.result!) }} · {{ rowSpeedTest(p)!.time }}</span>
                    <span v-else class="badge badge-muted" data-testid="strip-speedtest">未测速</span>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 待补测 -->
    <div v-if="pending.length" class="card">
      <div class="card-header"><div class="card-title">待补测代理</div></div>
      <div class="card-body text-sm">
        <div v-for="p in pending" :key="p" class="py-1"><code>{{ bindOf(p) }}</code></div>
      </div>
    </div>
  </div>

  <ContextMenu
    v-if="menu"
    :items="menu.items"
    :x="menu.x"
    :y="menu.y"
    @close="menu = null"
    @select="onMenuAction"
  />
</template>
