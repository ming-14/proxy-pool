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
  purityCache,
  purityScoreClass,
  usageTypeShort,
  type PurityEntry,
} from '@/composables/usePurity';
import { runSpeedTest, speedTestOf } from '@/composables/useSpeedTest';
import { GEO_REFRESH_MS, geoText, refreshStandbyGeo, standbyGeo } from '@/composables/useStandbyGeo';
import { refreshStandbySeries, seriesBuffers } from '@/composables/useStandbySeries';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import PurityTags from '@/components/PurityTags.vue';
import Sparkline from '@/components/Sparkline.vue';
import { bindOf, fmtAge, fmtBytes, fmtLatency } from '@/utils/format';
import { mergeKeepOrder } from '@/utils/merge';
import { hasQualityIssue, qualityFlags, type QualityFlags } from '@/utils/quality';
import { sparkStats, toRates, type NumericPoint } from '@/utils/series';
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
  // 锁定期间队列行不参与框选/多选（等价旧版"整卡禁用"；锁定行的按钮仍可用）
  isExcludedTarget: (e) => hasLock.value || !!(e.target as HTMLElement).closest('code, button'),
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
// 并在灰显行上叠一层选中高亮（既不可操作又看着像被选中）
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
  flags: QualityFlags;
}

const EMPTY_VIEW: RowView = {
  lat: [],
  latNote: '窗口内无探测',
  traffic: [],
  trafficNote: '窗口内无流量',
  flags: { failing: false, slow: false, unstable: false, dirty: false },
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
      flags: qualityFlags(row, stats.rate),
    });
  }
  return out;
});

function viewOf(row: StandbyEntry): RowView {
  return viewByBind.value.get(bindOf(row.proxy)) ?? EMPTY_VIEW;
}

/** 低质量标记（失败 / 慢 / 不稳 / 脏，阈值见 utils/quality.ts） */
function isFlagged(row: StandbyEntry): boolean {
  return hasQualityIssue(viewOf(row).flags);
}

/** 命中原因（行内小标签，让色条有解释） */
function flagText(row: StandbyEntry): string {
  const f = viewOf(row).flags;
  return [
    f.failing ? '失败' : '',
    f.slow ? '慢' : '',
    f.unstable ? '不稳' : '',
    f.dirty ? '脏' : '',
  ].filter(Boolean).join(' · ');
}

function geoTextOf(row: StandbyEntry): string | null {
  return geoText(standbyGeo.get(bindOf(row.proxy)));
}

/** 入队时长（锁定行的 joined_at 是锁定时刻，语义不同故不展示） */
function joinedTextOf(row: StandbyEntry): string | null {
  if (row.locked || row.joined_at === null) return null;
  return `入队 ${fmtAge(Date.now() / 1000 - row.joined_at)}`;
}

function purityEntryOf(row: StandbyEntry): PurityEntry | null | undefined {
  return purityCache.get(bindOf(row.proxy)) ?? row.purity;
}

// ---------------- 右键菜单 ----------------
function onContextmenu(e: MouseEvent) {
  if (window.getSelection()?.toString()) return; // 有文本选区：让出原生右键菜单（复制）
  const tr = (e.target as HTMLElement).closest('tbody tr');
  if (!tr) return;
  const proxy = (tr as HTMLElement).dataset.proxy;
  if (!proxy) return;
  e.preventDefault();
  if (!selection.has(proxy)) selection.selectOne(proxy);
  contextProxy.value = proxy;
  menu.value = { items: buildMenuItems(), x: e.clientX, y: e.clientY };
}

function buildMenuItems(): MenuItem[] {
  const children = Object.entries(groups.value)
    .filter(([, g]) => g.kind !== 'standby' && g.kind !== 'blacklist')
    .map(([name, g]) => ({ label: g.label, action: 'move', group: name, meta: String(g.count) }));
  const items: MenuItem[] = [
    { label: '移出', children },
    { label: '拉黑', action: 'blacklist' },
  ];
  if (selection.count() <= 1) items.push({ label: '测速', action: 'speedtest' });
  return items;
}

async function onMenuAction(item: MenuItem) {
  menu.value = null;
  const proxies = selection.getSelectedArray();
  const list = proxies.length ? proxies : [contextProxy.value];
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
function onTableMouseover(e: MouseEvent) {
  const tr = (e.target as HTMLElement).closest('tbody tr');
  hoveredProxy.value = tr ? ((tr as HTMLElement).dataset.proxy || null) : null;
}

function onTableMouseleave() {
  hoveredProxy.value = null;
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
            {{ hasLock ? '已锁定单一上游：无论心跳是否成功，只使用此代理' : 'pproxy 队列' }}
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
        @mouseover="onTableMouseover"
        @mouseleave="onTableMouseleave"
        @contextmenu="onContextmenu"
      >
        <div v-if="!displayStandby.length" class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
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
              :data-flagged="isFlagged(p) ? '1' : null"
              :class="{
                selected: selection.has(p.proxy),
                'row-locked': p.locked,
                'row-dimmed': hasLock && !p.locked,
                'row-flag': isFlagged(p),
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
                      <PurityTags :data="purityEntryOf(p)" />
                    </div>
                    <div class="proxy-meta">
                      <span>{{ p.protocol }}</span>
                      <span v-if="geoEnabled" :title="GEO_TIP">{{ geoTextOf(p) ?? '归属未知' }}</span>
                      <span v-if="joinedTextOf(p)">{{ joinedTextOf(p) }}</span>
                      <span
                        v-if="isFlagged(p)"
                        class="badge badge-danger"
                        :title="`低质量：${flagText(p)}`"
                      >{{ flagText(p) }}</span>
                    </div>
                  </div>
                  <div>{{ fmtLatency(p.latency) }}</div>
                  <div>{{ p.consecutive_fails ?? 0 }}</div>
                  <div class="prow-actions">
                    <button
                      class="btn btn-ghost btn-sm"
                      data-action="purity"
                      :disabled="purityEntryOf(p)?.status === 'checking'"
                      title="查询 IP 滥用置信度与类型（结果写入 30 天缓存）"
                      @click="checkPurity(p.proxy)"
                    >{{ purityEntryOf(p)?.status === 'checking' ? '检查中' : '纯洁' }}</button>
                    <button
                      class="btn btn-ghost btn-sm"
                      data-action="speedtest"
                      :disabled="speedTestOf(bindOf(p.proxy))?.status === 'running'"
                      title="Cloudflare 上传/下载测速（最多 10s）"
                      @click="runSpeedTest(bindOf(p.proxy))"
                    >{{ speedTestOf(bindOf(p.proxy))?.status === 'running' ? '测速中' : '测速' }}</button>
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
                    <Sparkline :series="[{ pts: viewOf(p).lat, color: 'var(--color-primary-600)' }]" log />
                  </div>
                  <div class="pstrip-chart" data-testid="strip-traffic">
                    <div class="pstrip-label">
                      <span>流量速率</span>
                      <span class="pstrip-note">{{ viewOf(p).trafficNote }}</span>
                    </div>
                    <Sparkline :series="viewOf(p).traffic" />
                  </div>
                  <div class="pstrip-side" data-testid="strip-side">
                    <template v-if="purityEntryOf(p)?.status === 'checking'">
                      <span class="badge badge-muted" data-testid="strip-purity">纯洁度检查中…</span>
                    </template>
                    <span v-else-if="purityEntryOf(p)?.error" class="badge badge-danger" data-testid="strip-purity">纯洁度检查失败</span>
                    <template v-else-if="purityEntryOf(p)?.abuse_confidence_score != null">
                      <span
                        class="badge"
                        :class="purityScoreClass(purityEntryOf(p)!.abuse_confidence_score!)"
                        data-testid="strip-purity"
                        title="滥用置信度（0-100，越低越干净）"
                      >滥用 {{ purityEntryOf(p)!.abuse_confidence_score }}</span>
                      <span class="badge badge-info" :title="purityEntryOf(p)!.usage_type || ''">
                        {{ usageTypeShort(purityEntryOf(p)!.usage_type || '') }}
                      </span>
                    </template>
                    <span v-else class="badge badge-muted" data-testid="strip-purity">纯洁度未检查</span>
                    <span v-if="speedTestOf(bindOf(p.proxy))?.status === 'running'" class="badge badge-muted" data-testid="strip-speedtest">测速中…（最多 10s）</span>
                    <span v-else-if="speedTestOf(bindOf(p.proxy))?.error" class="badge badge-danger" data-testid="strip-speedtest">
                      测速失败：{{ speedTestOf(bindOf(p.proxy))!.error }}
                    </span>
                    <span v-else-if="speedTestOf(bindOf(p.proxy))?.result" class="badge badge-info" data-testid="strip-speedtest">
                      ↓{{ speedTestOf(bindOf(p.proxy))!.result!.download?.best_mbps?.toFixed(1) ?? '-' }}
                      ↑{{ speedTestOf(bindOf(p.proxy))!.result!.upload?.best_mbps?.toFixed(1) ?? '-' }} Mbps
                      · {{ speedTestOf(bindOf(p.proxy))!.time }}
                    </span>
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
