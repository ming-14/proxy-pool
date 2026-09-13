<script setup lang="ts">
/**
 * 代理服务队列页：队列表格（框选/右键/固定/锁定/纯洁度）、待补测。
 * 锁定单一上游时锁定代理作为列表首行（行内「已锁定」标记，操作列只剩纯洁/解锁），
 * 其余行灰显且不可交互——不单独开锁定卡片。
 *
 * 旧版 934 行补丁集（文本选区冻结×2、hover 冻结、innerHTML 重建跳过、focus 跳过…）
 * 由 Vue 响应式渲染天然消解；仅保留"选中/拖拽/悬浮时冻结顺序"这一 UX 语义
 * （mergeKeepOrder 纯函数），且写动作后由 refreshQueueOrder 穿透冻结即时回正。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { useToastStore } from '@/stores/toast';
import { useRowSelection } from '@/composables/useRowSelection';
import { checkPurity, purityCache, type PurityEntry } from '@/composables/usePurity';
import {
  cancelShow,
  hideTooltip,
  pinTooltip,
  runSpeedTest,
  scheduleHide,
  scheduleShow,
  showTooltip,
  tooltipState,
} from '@/composables/useProxyTooltip';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import PurityTags from '@/components/PurityTags.vue';
import ProxyTooltip from '@/components/ProxyTooltip.vue';
import { bindOf, fmtLatency } from '@/utils/format';
import { mergeKeepOrder } from '@/utils/merge';
import type { MenuItem } from '@/types/ui';
import type { StandbyEntry } from '@/types/api';

const store = useStatusStore();
const toast = useToastStore();

const status = computed(() => store.status);
const standbyList = computed(() => status.value?.standby ?? []);
/** 是否已锁定单一上游（锁定行的其余队列行灰显且不可交互）——以后端 locked_proxy 为准，
 *  与列表内的 locked 行标记同源（api.py 由同一次 is_locked() 判定下发） */
const hasLock = computed(() => store.lockedProxy !== null);
const groups = computed(() => status.value?.groups ?? {});
const pending = computed(() => status.value?.pending_retries ?? []);

const tableWrapRef = ref<HTMLElement | null>(null);
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
  // 锁定期间队列行不参与框选/多选（等价旧版"整卡禁用"；锁定行的按钮与浮窗仍可用）
  isExcludedTarget: (e) => hasLock.value || !!(e.target as HTMLElement).closest('code, button'),
  onDragStart: hideTooltip, // 框选出现立即关浮窗
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

function purityOf(proxy: string): PurityEntry | undefined {
  return purityCache.get(bindOf(proxy));
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

function onMenuAction(item: MenuItem) {
  const pos = { x: menu.value?.x ?? 0, y: menu.value?.y ?? 0 };
  menu.value = null;
  const proxies = selection.getSelectedArray();
  const list = proxies.length ? proxies : [contextProxy.value];
  if (item.action === 'move' && item.group) {
    api.moveProxy(list, item.group)
      .then((d) => {
        toast.success('已移出', `已将 ${d.count} 个代理移到 ${groups.value[item.group!]?.label ?? item.group}`);
        selection.clear();
      })
      .catch((err: Error) => toast.error('移出失败', err.message));
  } else if (item.action === 'blacklist') {
    api.blacklist(list)
      .then((d) => {
        toast.success('已拉黑', `已将 ${d.count} 个代理加入黑名单`);
        selection.clear();
      })
      .catch((err: Error) => toast.error('拉黑失败', err.message));
  } else if (item.action === 'speedtest') {
    const bind = bindOf(contextProxy.value);
    showTooltip(bind, pos.x, pos.y, true);
    runSpeedTest(bind);
  }
}

// ---------------- 表格 code 交互（hover 浮窗 / 点击 pin / 拖拽隐藏） ----------------
function onTableMouseover(e: MouseEvent) {
  const tr = (e.target as HTMLElement).closest('tbody tr');
  hoveredProxy.value = tr ? ((tr as HTMLElement).dataset.proxy || null) : null;
  if (selection.isDragging()) {
    cancelShow();
    return;
  }
  const code = (e.target as HTMLElement).closest('code');
  if (!code) {
    cancelShow();
    return;
  }
  if (window.getSelection()?.toString()) {
    cancelShow();
    return;
  }
  const bind = bindOf(code.textContent || '');
  if (tooltipState.visible && tooltipState.bind === bind) return;
  scheduleShow(bind, e.clientX, e.clientY);
}

function onTableMouseleave() {
  hoveredProxy.value = null;
  cancelShow();
  scheduleHide();
}

function onTableMousedown(e: MouseEvent) {
  const code = (e.target as HTMLElement).closest('code');
  if (!code) return;
  const startX = e.clientX;
  const startY = e.clientY;
  let moved = false;
  const onMove = (ev: MouseEvent) => {
    if (!moved && Math.hypot(ev.clientX - startX, ev.clientY - startY) > 4) {
      moved = true;
      hideTooltip(); // 拖选文本：立即关浮窗
      document.removeEventListener('mousemove', onMove, true);
    }
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('mouseup', onUp, true);
    if (moved) return;
    const bind = bindOf(code.textContent || '');
    if (tooltipState.visible && tooltipState.bind === bind) pinTooltip();
  };
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('mouseup', onUp, true);
}

// pin 状态下点击浮窗外部 → 关闭
function onDocMousedown(e: MouseEvent) {
  if (!tooltipState.pinned || !tooltipState.visible) return;
  const target = e.target as HTMLElement;
  if (target.closest('.proxy-tooltip')) return;
  if (target.closest('#sb-table code')) return; // 表格 code 的 pin 由 onTableMousedown 处理
  hideTooltip();
}

// 主区域滚动：非 pin 时关闭浮窗（fixed 定位不跟随滚动）
function onMainScroll() {
  if (tooltipState.visible && !tooltipState.pinned) hideTooltip();
}

// 队列刷新后：hover 的表格代理已离队且未 pin → 销毁浮窗
watch(displayStandby, (list) => {
  if (!tooltipState.visible || tooltipState.pinned || !tooltipState.bind) return;
  if (!list.some((p) => bindOf(p.proxy) === tooltipState.bind)) hideTooltip();
});

onMounted(() => {
  document.addEventListener('mousedown', onDocMousedown, true);
  document.getElementById('main-content')?.addEventListener('scroll', onMainScroll, { passive: true });
});

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocMousedown, true);
  document.getElementById('main-content')?.removeEventListener('scroll', onMainScroll);
  hideTooltip(); // 离开页面销毁浮窗（模块级状态跨路由保留测速结果）
});
</script>

<template>
  <div class="space-y-5">
    <!-- 队列卡片：锁定期间锁定行置顶高亮，其余行灰显且不可交互 -->
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">代理服务队列</div>
          <div class="card-subtitle">
            {{ hasLock ? '已锁定单一上游：无论心跳是否成功，只使用此代理' : 'pproxy 队列' }}
          </div>
        </div>
      </div>
      <div
        id="sb-table"
        ref="tableWrapRef"
        data-testid="sb-table"
        class="table-wrap border-0 rounded-t-none"
        @mouseover="onTableMouseover"
        @mouseleave="onTableMouseleave"
        @mousedown="onTableMousedown"
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
              :class="{
                selected: selection.has(p.proxy),
                'row-locked': p.locked,
                'row-dimmed': hasLock && !p.locked,
              }"
            >
              <td class="text-[var(--color-t-muted)] w-12">{{ i + 1 }}</td>
              <td>
                <code>{{ p.proxy }}</code>
                <span
                  v-if="p.locked"
                  class="badge badge-warning ml-2"
                  title="无论心跳是否成功，只使用此代理"
                >已锁定</span>
                <PurityTags :data="purityOf(p.proxy) ?? p.purity" />
              </td>
              <td>{{ fmtLatency(p.latency) }}</td>
              <td>{{ p.consecutive_fails ?? 0 }}</td>
              <td class="text-right whitespace-nowrap">
                <button
                  class="btn btn-ghost btn-sm"
                  data-action="purity"
                  :disabled="purityOf(p.proxy)?.status === 'checking'"
                  @click="checkPurity(p.proxy)"
                >
                  {{ purityOf(p.proxy)?.status === 'checking' ? '检查中' : '纯洁' }}
                </button>
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

  <ProxyTooltip />
</template>
