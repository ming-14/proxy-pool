<script setup lang="ts">
/**
 * 代理组页：组选择、关键字过滤、排序、虚拟滚动、框选/多选、右键批量操作、导出、添加。
 *
 * 虚拟滚动用 @tanstack/vue-virtual：scrollMargin = 行0在滚动容器内容空间的偏移，
 * 上下 spacer 行高度 = 首/末可见项 start - scrollMargin；旧版手工 spacer/滚动跳转/
 * trim/行高估算补丁全部移除。行高亮由选中集 class 绑定驱动，无 DOM toggle。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useVirtualizer } from '@tanstack/vue-virtual';
import { api, errText } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { useToastStore } from '@/stores/toast';
import { useRowSelection } from '@/composables/useRowSelection';
import { runAction } from '@/composables/runAction';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import Icon from '@/components/ui/Icon.vue';
import type { IconName } from '@/components/ui/icons';
import AddProxyModal from '@/components/AddProxyModal.vue';
import { displayCountryName, fmtDuration, fmtLatency, fmtDateTime } from '@/utils/format';
import { buildCsv, downloadFile } from '@/utils/csv';
import type { MenuItem } from '@/types/ui';
import type { GroupDetail, GroupProxyEntry } from '@/types/api';

type SortKey =
  | 'proxy' | 'consecutive_fails' | 'latency' | 'expires_in' | 'joined_at' | 'geo' | 'source_group';

const store = useStatusStore();
const toast = useToastStore();

const groups = computed(() => store.status?.groups ?? {});
const currentGroup = ref<string | null>(null);
const detail = ref<GroupDetail | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const keyword = ref('');
const sortKey = ref<SortKey>('proxy');
const sortDesc = ref(false);
const showAdd = ref(false);
const menu = ref<{ items: MenuItem[]; x: number; y: number } | null>(null);
const contextProxy = ref(''); // 右键目标行（批量操作空选中时的兜底）
const showBackTop = ref(false);

const tableWrapRef = ref<HTMLElement | null>(null);
const tbodyRef = ref<HTMLElement | null>(null);
const scrollMargin = ref(0);

const COLS: { key: SortKey; label: string }[] = [
  { key: 'proxy', label: '代理' },
  { key: 'consecutive_fails', label: '连续失败' },
  { key: 'latency', label: '延迟' },
  { key: 'expires_in', label: '过期倒计时' },
  { key: 'joined_at', label: '加入时间' },
  { key: 'geo', label: 'IP 归属' },
  { key: 'source_group', label: '来源' },
];

// ---------------- 数据加载 ----------------
watch(groups, (g) => {
  if (!currentGroup.value && Object.keys(g).length) currentGroup.value = Object.keys(g)[0];
}, { immediate: true });

watch(
  currentGroup,
  async (g) => {
    if (!g) return;
    loading.value = true;
    loadError.value = null;
    detail.value = null;
    try {
      detail.value = await api.group(g);
    } catch (e) {
      loadError.value = errText(e);
    } finally {
      loading.value = false;
      await nextTick();
      updateScrollMargin();
    }
  },
  { immediate: true },
); // immediate：初始组由上方 groups watch 同步设定，需首轮即触发加载

async function reload() {
  const g = currentGroup.value;
  if (!g) return;
  loading.value = true;
  try {
    detail.value = await api.group(g);
  } catch (e) {
    toast.error('加载失败', errText(e));
  } finally {
    loading.value = false;
  }
}

// ---------------- 过滤 + 排序 ----------------
function geoSortKey(p: GroupProxyEntry): string {
  if (!p.geo) return '\uffff';
  return [p.geo.country || '', p.geo.province || '', p.geo.city || ''].join('\u0000');
}

function sortValue(p: GroupProxyEntry, key: SortKey): number | string {
  switch (key) {
    case 'consecutive_fails': return p.consecutive_fails ?? 0;
    case 'latency': return p.latency ?? Infinity;
    case 'expires_in': return p.expires_in ?? Infinity;
    case 'joined_at': return p.joined_at ?? 0;
    case 'geo': return geoSortKey(p);
    case 'source_group': return p.source_group ?? '';
    default: return p.proxy;
  }
}

const rows = computed<GroupProxyEntry[]>(() => {
  if (!detail.value) return [];
  let list = detail.value.proxies;
  const kw = keyword.value.trim().toLowerCase();
  if (kw) list = list.filter((p) => p.proxy.toLowerCase().includes(kw));
  const desc = sortDesc.value;
  return [...list].sort((a, b) => {
    const av = sortValue(a, sortKey.value);
    const bv = sortValue(b, sortKey.value);
    if (av < bv) return desc ? 1 : -1;
    if (av > bv) return desc ? -1 : 1;
    return 0;
  });
});

// ---------------- 虚拟滚动 ----------------
const scrollEl = () => document.getElementById('main-content');

const virtualizer = useVirtualizer(
  computed(() => ({
    count: rows.value.length,
    getScrollElement: scrollEl,
    estimateSize: () => 41,
    overscan: 8,
    scrollMargin: scrollMargin.value,
  })),
);

const virtualItems = computed(() => virtualizer.value.getVirtualItems());
const padTop = computed(() => {
  const items = virtualItems.value;
  return items.length ? items[0].start - scrollMargin.value : 0;
});
const padBottom = computed(() => {
  const items = virtualItems.value;
  if (!items.length) return 0;
  return virtualizer.value.getTotalSize() - scrollMargin.value - items[items.length - 1].end;
});

/**
 * 行0在滚动内容空间的偏移 = tbody 在滚动内容里的位置（列表起点，非当前渲染起点）。
 *
 * **不能减去顶部 spacer 高**：spacer 是 tbody 的首个子元素、只把渲染行往下推，tbody 顶本身就在
 * 行0的位置；而 vue-virtual 的 item.start 已含 scrollMargin、区间判定直接拿 scrollTop 与之比较，
 * 故 scrollMargin 必须严格等于列表起点（padTop 也由 items[0].start - scrollMargin 反推，减了会
 * 自我抵消）。多减会让每次调用都往下漂移一个 spacer：滚动状态下改窗口尺寸后渲染行被推到视口下方
 * （整屏空白 + 总高膨胀，回归用例 TestGroups::test_scroll_margin_survives_resize），而顶部调用时
 * padTop = 0、减与不减等价，所以此前没暴露。
 */
function updateScrollMargin() {
  const s = scrollEl();
  const tb = tbodyRef.value;
  if (!s || !tb) return;
  scrollMargin.value = tb.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop;
}

/** 框选坐标 → 数据索引（内容空间二分，可跨未渲染区域） */
function indexAtY(clientY: number): number {
  const s = scrollEl();
  if (!s || !rows.value.length) return -1;
  const contentOffset = clientY - s.getBoundingClientRect().top + s.scrollTop;
  const item = virtualizer.value.getVirtualItemForOffset(contentOffset);
  return item ? item.index : 0;
}

// ---------------- 行选择 ----------------
// 组件仅在 groups 路由激活时挂载，无需页面级 isActive 判定
const selection = useRowSelection<GroupProxyEntry>({
  container: tableWrapRef,
  scrollEl,
  getRows: () => rows.value,
  proxyOf: (r) => r.proxy,
  indexAtY,
  isExcludedTarget: (e) => !!(e.target as HTMLElement).closest('button, input, select'),
});

// 视图变更（切组/改过滤）清空选中：选中集与可见行不一致时批量操作易误伤（与旧版一致）
watch(currentGroup, () => selection.clear());
watch(keyword, () => selection.clear());

function onContextmenu(e: MouseEvent) {
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
  const kind = currentGroup.value ? groups.value[currentGroup.value]?.kind : 'normal';
  const deleteItem: MenuItem = { label: '删除', action: 'delete', danger: true };
  if (kind === 'blacklist') {
    return [{ label: '还原', action: 'restore' }, deleteItem];
  }
  const moveChildren = Object.entries(groups.value)
    .filter(([name, g]) => name !== currentGroup.value && g.kind !== 'blacklist' && g.kind !== 'rejected')
    .map(([name, g]) => ({ label: g.label, action: 'move', group: name, meta: String(g.count) }));
  return [
    { label: '移动', children: moveChildren },
    { label: '拉黑', action: 'blacklist' },
    deleteItem,
  ];
}

// ---------------- 批量动作 ----------------
/** 写动作收尾：清空选中 + 重载当前组 */
function resetAndReload(): void {
  selection.clear();
  void reload();
}

async function doMove(proxies: string[], targetGroup: string) {
  await runAction(() => api.moveProxy(proxies, targetGroup), {
    ok: (d) => ({
      title: '已移动',
      message: `已将 ${d.count} 个代理移到 ${groups.value[targetGroup]?.label ?? targetGroup}`,
    }),
    failTitle: '移动失败',
    after: resetAndReload,
  });
}

async function doBlacklist(proxies: string[]) {
  await runAction(() => api.blacklist(proxies), {
    ok: (d) => ({ title: '已拉黑', message: `已将 ${d.count} 个代理加入黑名单` }),
    failTitle: '拉黑失败',
    after: resetAndReload,
  });
}

async function doDelete(proxies: string[]) {
  await runAction(() => api.removeProxy(proxies, currentGroup.value!), {
    ok: (d) => ({ title: '已删除', message: `已从当前组移除 ${d.count} 个代理` }),
    failTitle: '删除失败',
    after: resetAndReload,
  });
}

/** 还原：按 source_group 分组各自回来源组；无来源组记录的跳过并提示 */
async function doRestore(proxies: string[]) {
  const byTarget: Record<string, string[]> = {};
  let skipped = 0;
  for (const p of proxies) {
    const target = detail.value?.proxies.find((x) => x.proxy === p)?.source_group;
    if (!target) {
      skipped++;
      continue;
    }
    (byTarget[target] ??= []).push(p);
  }
  if (!Object.keys(byTarget).length) {
    toast.error('还原失败', '所选代理均无来源组记录，无法还原');
    return;
  }
  await runAction(
    async () => {
      let total = 0;
      for (const [target, list] of Object.entries(byTarget)) {
        total += (await api.moveProxy(list, target)).count;
      }
      return { total };
    },
    {
      ok: ({ total }) => ({
        title: '已还原',
        message: `已还原 ${total} 个代理${skipped ? `，${skipped} 个无来源组被跳过` : ''}`,
      }),
      failTitle: '还原失败',
      after: resetAndReload,
    },
  );
}

function onMenuAction(item: MenuItem, fallbackProxy: string) {
  menu.value = null;
  const proxies = selection.getSelectedArray();
  const list = proxies.length ? proxies : [fallbackProxy];
  if (item.action === 'move' && item.group) doMove(list, item.group);
  else if (item.action === 'blacklist') doBlacklist(list);
  else if (item.action === 'restore') doRestore(list);
  else if (item.action === 'delete') doDelete(list);
}

// ---------------- 导出 ----------------
function geoExport(p: GroupProxyEntry): string {
  if (!p.geo) return '';
  return [p.geo.country || '', p.geo.province || '', p.geo.city || ''].filter(Boolean).join('·');
}

function exportCsv() {
  if (!rows.value.length) {
    toast.warning('无数据', '当前没有可导出的代理');
    return;
  }
  const header = ['proxy', 'consecutive_fails', 'latency', 'expires_in', 'joined_at', 'geo', 'source_group'];
  const body = rows.value.map((p) => [
    p.proxy,
    p.consecutive_fails ?? 0,
    p.latency ?? '',
    p.expires_in ?? '',
    new Date((p.joined_at || 0) * 1000).toISOString(),
    geoExport(p),
    p.source_group ?? '',
  ]);
  downloadFile(buildCsv(header, body), `${currentGroup.value}.csv`, 'text/csv');
}

function exportJson() {
  if (!rows.value.length) {
    toast.warning('无数据', '当前没有可导出的代理');
    return;
  }
  downloadFile(JSON.stringify(rows.value, null, 2), `${currentGroup.value}.json`, 'application/json');
}

// ---------------- 添加 ----------------
function onAddSubmit(proxies: string[], done: () => void) {
  api.addProxy(currentGroup.value!, proxies)
    .then((d) => {
      // 协议不在支持集合（如 socks4）的项由后端拒绝，单独提示，不混进成功数
      if (d.rejected.length) {
        toast.warning('部分未添加', `${d.rejected.length} 个代理协议不受支持，已跳过`);
      }
      if (d.added) {
        toast.success('已添加', `成功添加 ${d.added} 个代理`);
      }
      showAdd.value = false;
      reload();
    })
    .catch((e: Error) => toast.error('添加失败', e.message))
    .finally(done);
}

// ---------------- 回到顶部 ----------------
function onMainScroll() {
  const s = scrollEl();
  showBackTop.value = !!s && s.scrollTop > 500;
}

onMounted(() => {
  scrollEl()?.addEventListener('scroll', onMainScroll, { passive: true });
  window.addEventListener('resize', updateScrollMargin);
  updateScrollMargin();
});

onBeforeUnmount(() => {
  scrollEl()?.removeEventListener('scroll', onMainScroll);
  window.removeEventListener('resize', updateScrollMargin);
});

/** 表头排序指示图标（未排序列显示双向箭头；原为 ↕↑↓ 字形，收编为同族 SVG） */
function sortIcon(key: SortKey): IconName {
  if (sortKey.value !== key) return 'sort';
  return sortDesc.value ? 'sort-desc' : 'sort-asc';
}

function sortBy(key: SortKey) {
  if (sortKey.value === key) sortDesc.value = !sortDesc.value;
  else {
    sortKey.value = key;
    sortDesc.value = false;
  }
  virtualizer.value.scrollToOffset(0);
}

function groupLabelOf(g: string | null): string {
  if (!g) return '';
  return groups.value[g]?.label ?? g;
}

/** 归属单元格文本（国家 · 市/省）与完整 title（null 安全） */
function geoShort(p: GroupProxyEntry): string {
  const geo = p.geo;
  if (!geo) return '';
  const country = displayCountryName(geo.country || '未知');
  const parts = [geo.province, geo.city].filter(Boolean);
  return parts.length ? `${country} · ${parts.join('')}` : country;
}

function geoFull(p: GroupProxyEntry): string {
  const geo = p.geo;
  if (!geo) return '';
  return [displayCountryName(geo.country || '未知'), geo.province, geo.city].filter(Boolean).join(' · ');
}

// 与旧版逐字一致：空列表也显示范围（“共 0 条 · 显示第 1-0 条”）
const infoText = computed(() => {
  const total = rows.value.length;
  const items = virtualItems.value;
  const first = items.length ? items[0].index + 1 : 1;
  const last = items.length ? items[items.length - 1].index + 1 : 0;
  return `共 ${total} 条 · 显示第 ${first}-${last} 条`;
});
</script>

<template>
  <div class="space-y-5">
    <div class="card">
      <div class="card-header flex-wrap gap-3">
        <div class="flex items-center gap-3 flex-wrap">
          <label class="form-label mb-0">选择分组</label>
          <select v-model="currentGroup" data-testid="grp-select" class="select min-w-[180px]">
            <option v-for="(g, name) in groups" :key="name" :value="name">
              {{ g.label }} ({{ g.count }})
            </option>
          </select>
          <input v-model="keyword" data-testid="grp-filter" class="input w-48" placeholder="关键字过滤">
        </div>
        <div class="flex items-center gap-2">
          <button data-testid="grp-add-btn" class="btn btn-primary btn-sm" @click="showAdd = true">添加</button>
          <button data-testid="grp-export-csv" class="btn btn-ghost btn-sm" @click="exportCsv">导出 CSV</button>
          <button data-testid="grp-export-json" class="btn btn-ghost btn-sm" @click="exportJson">导出 JSON</button>
        </div>
      </div>
      <div class="card-body">
        <div class="table-wrap mb-4" ref="tableWrapRef" data-testid="grp-table" @contextmenu="onContextmenu">
          <!-- 未选组 -->
          <div v-if="!currentGroup" class="p-8 text-center text-[var(--color-t-muted)]">选择一个分组查看代理明细</div>
          <!-- 加载中骨架 -->
          <div v-else-if="loading" class="p-8 space-y-3">
            <div class="skeleton skeleton-title" />
            <div v-for="i in 3" :key="i" class="skeleton skeleton-line" />
          </div>
          <div v-else-if="loadError" class="p-6 text-center text-[var(--color-danger)]">加载失败：{{ loadError }}</div>
          <table v-else>
            <thead>
              <tr>
                <th
                  v-for="c in COLS"
                  :key="c.key"
                  class="sortable"
                  :class="{ 'sort-active': sortKey === c.key }"
                  @click="sortBy(c.key)"
                >
                  {{ c.label }}
                  <span class="sort-icon inline-flex align-middle">
                    <Icon :name="sortIcon(c.key)" class="w-3 h-3" />
                  </span>
                </th>
              </tr>
            </thead>
            <tbody ref="tbodyRef">
              <tr v-if="padTop > 0" :style="{ height: padTop + 'px' }">
                <td :colspan="COLS.length" style="padding:0;border:0" />
              </tr>
              <tr
                v-for="vi in virtualItems"
                :key="vi.index"
                :data-index="vi.index"
                :data-idx="vi.index"
                :data-proxy="rows[vi.index].proxy"
                :class="{ selected: selection.has(rows[vi.index].proxy) }"
                :ref="(el) => virtualizer.measureElement(el as HTMLElement)"
              >
                <td><code>{{ rows[vi.index].proxy }}</code></td>
                <td>{{ rows[vi.index].consecutive_fails ?? 0 }}</td>
                <td>{{ fmtLatency(rows[vi.index].latency) }}</td>
                <td>{{ rows[vi.index].expires_in == null ? '-' : fmtDuration(rows[vi.index].expires_in) }}</td>
                <td class="text-[var(--color-t-muted)]">{{ fmtDateTime(rows[vi.index].joined_at) }}</td>
                <td>
                  <span v-if="rows[vi.index].geo" :title="geoFull(rows[vi.index])">{{ geoShort(rows[vi.index]) }}</span>
                  <span v-else class="text-[var(--color-t-muted)]">-</span>
                </td>
                <td>
                  <span v-if="rows[vi.index].source_group">{{ groupLabelOf(rows[vi.index].source_group) }}</span>
                  <span v-else class="text-[var(--color-t-muted)]">-</span>
                </td>
              </tr>
              <tr v-if="!rows.length">
                <td :colspan="COLS.length" class="text-center text-[var(--color-t-muted)] py-8">无匹配代理</td>
              </tr>
              <tr v-if="padBottom > 0" :style="{ height: padBottom + 'px' }">
                <td :colspan="COLS.length" style="padding:0;border:0" />
              </tr>
            </tbody>
          </table>
        </div>
        <div data-testid="grp-info" class="text-sm text-[var(--color-t-muted)]">{{ infoText }}</div>
      </div>
    </div>

    <button v-show="showBackTop" class="back-to-top-btn" title="回到顶部" @click="scrollEl()?.scrollTo({ top: 0, behavior: 'smooth' })">
      <Icon name="chevron-up" />
    </button>

    <ContextMenu
      v-if="menu"
      :items="menu.items"
      :x="menu.x"
      :y="menu.y"
      @close="menu = null"
      @select="onMenuAction($event, contextProxy)"
    />

    <AddProxyModal
      v-if="showAdd"
      :group-label="groupLabelOf(currentGroup)"
      @close="showAdd = false"
      @submit="onAddSubmit"
    />
  </div>
</template>
