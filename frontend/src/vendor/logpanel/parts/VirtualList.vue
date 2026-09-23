<script setup lang="ts" generic="E extends object">
/**
 * 虚拟列表：窗口计算、端部锚定交给 TanStack Virtual；行高由本组件的测高池全量预量。
 *
 * 行高必须全量预量：正文换行使行高不一致，只量可视行的话未渲染区域永远按估算值撑高，
 * 总高会随滚动不断被修正 —— 原生滚动条滑块按 scrollTop/scrollHeight 映射，总高一变
 * 滑块就与鼠标位置脱节（拖动滚动条"跟不上鼠标"）。测高池把每行量一遍，总高一次到位。
 *
 * - 端部锚定：anchorTo: 'end' —— 头部淘汰/末尾追加后按 key 锚定视口首行，滚动位置不漂移；
 * - 贴底跟随：新条目到达与内容高度变化都经同一个 stickToEnd 收敛到底部（库内 followOnAppend
 *   不可用，原因见 stickToEnd 注释）；跟随意图由滚动手势维护，上滚即停、滚回底部即恢复。
 *   程序侧滚动只发生在跟随或锚定语义内，不会把用户手动关闭的跟随顶回去。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { useVirtualizer } from '@tanstack/vue-virtual';
import type { LogPanelHooks, LogPanelKey, LogPanelRules } from '../types';
import type { Matcher } from '../utils/filter';
import LogLine from './LogLine.vue';

/** 未量行的初始兜底行高（px）：量到行后改用已量行的累计均值 */
const ROW_ESTIMATE = 22;
/** 视口外多渲染的行数 */
const OVERSCAN = 4;
/** 贴底判定阈值（px） */
const END_THRESHOLD = 5;
/** 单批测高行数：一批只触发一次布局 */
const MEASURE_BATCH = 64;
/** 单批取行的时间预算（ms），以及单帧推进测高的总预算（ms） */
const MEASURE_BATCH_BUDGET = 4;
const MEASURE_FRAME_BUDGET = 10;

/** 测高池里的一行：id 含展开态（同一行展开前后高度不同） */
interface PoolRow<E> {
  id: string;
  key: LogPanelKey;
  entry: E;
  expanded: boolean;
}

const props = defineProps<{
  entries: E[];
  rules: LogPanelRules<E>;
  hooks?: LogPanelHooks<E>;
  matcher: Matcher;
  keyOf: (entry: E) => LogPanelKey;
  expandedKeys: ReadonlySet<LogPanelKey>;
}>();

const emit = defineEmits<{ (e: 'toggle', entry: E): void }>();

/** 是否跟随新增日志（面板持有并持久化） */
const follow = defineModel<boolean>('follow', { required: true });

const scrollEl = ref<HTMLElement | null>(null);
const sizerEl = ref<HTMLElement | null>(null);

/** 条目与 key 一起取，保证 count / getItemKey / 渲染行读到同一份快照 */
const source = computed(() => ({
  entries: props.entries,
  keys: props.entries.map((entry) => props.keyOf(entry)),
}));

/** 未量行的兜底行高：已量行的累计均值（取最后一批会让尾部几行主导估算值） */
let estSum = 0;
let estCount = 0;
let estHeight = ROW_ESTIMATE;

const virtualizer = useVirtualizer(
  computed(() => {
    // 快照必须与 count 同步绑定：库在更新前后会用各自那轮 options 的 getItemKey 比对首尾 key，
    // 若这里读实时数据，旧 options 也会读到新 key，环形淘汰就被判成"没变化"而不做位置补偿
    const snapshot = source.value;
    return {
      count: snapshot.entries.length,
      getScrollElement: () => scrollEl.value,
      estimateSize: () => estHeight,
      getItemKey: (index: number) => snapshot.keys[index] ?? index,
      overscan: OVERSCAN,
      scrollEndThreshold: END_THRESHOLD,
      anchorTo: 'end' as const,
      measureElement: (element: Element) => (element as HTMLElement).offsetHeight,
    };
  }),
);

const rows = computed(() => {
  const { entries, keys } = source.value;
  const render: { index: number; start: number; key: LogPanelKey; entry: E; expanded: boolean }[] = [];
  for (const item of virtualizer.value.getVirtualItems()) {
    const entry = entries[item.index];
    if (entry === undefined) break; // 与库内范围错位的那一帧：越界行不渲染
    const key = keys[item.index];
    render.push({ index: item.index, start: item.start, key, entry, expanded: props.expandedKeys.has(key) });
  }
  return render;
});

function onScroll(): void {
  const el = scrollEl.value;
  if (!el) return;
  // 直接量 DOM：库内 scrollOffset 由其自身的滚动监听更新，与本监听器的执行顺序不确定
  const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight <= END_THRESHOLD;
  if (follow.value && !atEnd) {
    // 内容刚增长时位置还没跟上（程序侧贴底在同帧稍后才写），此时"不在末尾"不代表用户上滚；
    // 隔一帧复核：真上滚会保持不在末尾，而增长触发的贴底已经把位置带到末尾。
    if (!verifyFrame) verifyFrame = requestAnimationFrame(verifyFollow);
    return;
  }
  if (!follow.value && atEnd) follow.value = true;
}

/** 复核跟随意图：一帧后仍不在末尾才判定用户上滚 */
function verifyFollow(): void {
  verifyFrame = 0;
  const el = scrollEl.value;
  if (!el || !follow.value) return;
  if (el.scrollHeight - el.scrollTop - el.clientHeight > END_THRESHOLD) follow.value = false;
}

/**
 * 贴底跟随。不能用库的 followOnAppend：Vue 适配器在选项变更时立即调 _willUpdate，此刻本框架
 * 尚未提交 DOM（撑高块还是旧高度），按 'end' 算出的目标偏移是过期的，写回反而把视口拉到上面。
 * 因此等本次提交完成后再算。落点交由库自己维护（它会在后续帧把目标对齐到末尾）。
 */
async function stickToEnd(): Promise<void> {
  await nextTick();
  if (follow.value) virtualizer.value.scrollToEnd();
}

watch(() => props.entries, () => {
  void stickToEnd();
  scheduleMeasure(); // 新条目/过滤结果变化：把没量过的行补量上
});

// 测高池：池内行不参与滚动（零高 + 裁剪），只用于读 offsetHeight。
const poolRows = shallowRef<PoolRow<E>[]>([]);
const poolEl = ref<HTMLElement | null>(null);
/** 已量过的行 id：环形缓冲会不断换 key，超出视图规模时按当前视图裁剪 */
const measuredIds = new Set<string>();
/** 扫描游标：环形推进，避免每批都从 0 重扫 */
let measureCursor = 0;
let measureFrame = 0;
let verifyFrame = 0;
let measuring = false;
let disposed = false;

/** 行高缓存键：同一行展开前后高度不同 */
function heightId(key: LogPanelKey, expanded: boolean): string {
  return `${key}:${expanded ? 1 : 0}`;
}

/** 已量集合随环形缓冲 churn 增长，超过视图规模两倍时按当前视图裁剪 */
function pruneMeasured(keys: LogPanelKey[]): void {
  const keep = new Set<string>();
  for (const key of keys) keep.add(heightId(key, props.expandedKeys.has(key)));
  for (const id of measuredIds) if (!keep.has(id)) measuredIds.delete(id);
}

/** 取一批未量过的行；扫完一整轮都没取到即为量完 */
function takeBatch(): PoolRow<E>[] {
  const { entries, keys } = source.value;
  const count = entries.length;
  const batch: PoolRow<E>[] = [];
  if (!count) return batch;
  if (measureCursor >= count) measureCursor = 0; // 过滤把视图缩短后游标可能越界
  if (measuredIds.size > count * 2) pruneMeasured(keys);
  const deadline = performance.now() + MEASURE_BATCH_BUDGET;
  for (let scanned = 0; scanned < count && batch.length < MEASURE_BATCH; scanned++) {
    if (batch.length >= MEASURE_BATCH / 2 && performance.now() > deadline) break;
    const index = measureCursor;
    measureCursor = (measureCursor + 1) % count;
    const key = keys[index];
    const expanded = props.expandedKeys.has(key);
    const id = heightId(key, expanded);
    if (measuredIds.has(id)) continue;
    batch.push({ id, key, entry: entries[index], expanded });
  }
  return batch;
}

/** 写回一批实测行高：下标按当前视图重新解析（取批到量完之间视图可能已变） */
function applyBatch(batch: PoolRow<E>[], heights: number[]): void {
  const { keys } = source.value;
  const indexByKey = new Map<LogPanelKey, number>();
  for (let i = 0; i < keys.length; i++) indexByKey.set(keys[i], i);
  const list = virtualizer.value;
  for (let i = 0; i < batch.length; i++) {
    const height = heights[i];
    if (!(height > 0)) continue; // 未布局（面板隐藏等）：留待下轮
    const index = indexByKey.get(batch[i].key);
    if (index === undefined) continue; // 已被过滤或淘汰
    measuredIds.add(batch[i].id);
    estSum += height;
    estCount += 1;
    estHeight = Math.max(1, Math.round(estSum / estCount));
    list.resizeItem(index, height);
  }
}

/** 逐帧推进全量测高：一帧内按预算跑多批，量完为止 */
function scheduleMeasure(): void {
  if (disposed || measureFrame !== 0 || measuring) return;
  // 面板隐藏（v-show）时容器无宽度、池内行高恒为 0：不排空转，重新显示后由尺寸监听触发
  if (!scrollEl.value?.clientWidth) return;
  measureFrame = requestAnimationFrame(() => {
    measureFrame = 0;
    void measurePass();
  });
}

async function measurePass(): Promise<void> {
  measuring = true;
  let more = true;
  const deadline = performance.now() + MEASURE_FRAME_BUDGET;
  try {
    while (more && !disposed && performance.now() < deadline) {
      const batch = takeBatch();
      more = batch.length > 0;
      if (!more) break;
      poolRows.value = batch;
      await nextTick(); // 等池内行提交后再读布局
      const pool = poolEl.value?.children;
      // 面板隐藏（v-show）时容器无宽度、行高恒为 0：本轮作废（不排下一帧，免得空转），
      // 重新显示后由尺寸监听再触发
      if (!pool || pool.length !== batch.length || !scrollEl.value?.clientWidth) {
        more = false;
        poolRows.value = [];
        measureCursor = 0;
        return;
      }
      const heights = new Array<number>(batch.length);
      for (let i = 0; i < batch.length; i++) heights[i] = (pool[i] as HTMLElement).offsetHeight;
      applyBatch(batch, heights);
      poolRows.value = [];
    }
  } finally {
    measuring = false;
    if (more) scheduleMeasure(); // 本帧预算用尽：下一帧接着量
  }
}

/**
 * 尺寸变化都要贴底：
 * - 撑高块高度变化：新条目、行高实测修正（估算 → 实测）、面板由隐藏转为显示（0 → N）；
 * - 滚动容器自身高度变化：窗口或抽屉高度变化时 scrollTop 不变而可视高度变大，跟随态下
 *   末行会落到视口下方——内容高度没变，只盯撑高块发现不了。
 * 只有撑高块变化才补跑测高（容器高度变化与换行无关）。
 */
let sizeObserver: ResizeObserver | null = null;
onMounted(() => {
  const sizer = sizerEl.value;
  if (!sizer) return;
  sizeObserver = new ResizeObserver((entries) => {
    void stickToEnd();
    if (entries.some((entry) => entry.target === sizer)) scheduleMeasure(); // 由隐藏转为显示时补跑测高
  });
  sizeObserver.observe(sizer);
  if (scrollEl.value) sizeObserver.observe(scrollEl.value);
  scheduleMeasure();
});

onBeforeUnmount(() => {
  disposed = true;
  if (measureFrame !== 0) cancelAnimationFrame(measureFrame);
  if (verifyFrame !== 0) cancelAnimationFrame(verifyFrame);
  sizeObserver?.disconnect();
});

defineExpose({
  /** 窗口尺寸变化后重测行高（宽度变了，换行行数随之变）：全部行高作废，重跑测高 */
  remeasure: () => {
    measuredIds.clear();
    measureCursor = 0;
    scheduleMeasure();
  },
  /** 手动开启跟随时立即贴底（高度未变，不会触发内容高度监听） */
  toEnd: stickToEnd,
});
</script>

<template>
  <div ref="scrollEl" class="lp-list" @scroll.passive="onScroll">
    <div ref="sizerEl" class="lp-list-inner" :style="{ height: `${virtualizer.getTotalSize()}px` }">
      <div
        v-for="row in rows"
        :key="row.key"
        :ref="(el) => virtualizer.measureElement(el as HTMLElement | null)"
        :data-index="row.index"
        class="lp-row"
        :style="{ transform: `translateY(${row.start}px)` }"
      >
        <LogLine
          :entry="row.entry"
          :rules="rules"
          :hooks="hooks"
          :matcher="matcher"
          :expanded="row.expanded"
          @toggle="emit('toggle', row.entry)"
        />
      </div>

      <!-- 测高池：零高 + 裁剪，不占滚动空间也不参与滚动溢出；渲染结构必须与行完全一致 -->
      <div ref="poolEl" class="lp-measure" aria-hidden="true">
        <div v-for="row in poolRows" :key="row.id">
          <LogLine
            :entry="row.entry"
            :rules="rules"
            :hooks="hooks"
            :matcher="matcher"
            :expanded="row.expanded"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lp-list {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  font-family: var(--lp-font-mono-c);
  font-size: 11.5px;
  line-height: 1.5;
  /* 纵向滚动留给列表，横向手势不在此起手；滚动到端部不把滚动链传给背后的页面 */
  touch-action: pan-y;
  overscroll-behavior: contain;
  /* 虚拟列表自己维护锚定，禁用浏览器滚动锚定以免相互拉扯（与代理组虚拟表格同因） */
  overflow-anchor: none;
  scrollbar-color: var(--lp-scrollbar-c) transparent;
  scrollbar-width: thin;
}

.lp-list::-webkit-scrollbar {
  width: 8px;
}
.lp-list::-webkit-scrollbar-track {
  background: transparent;
}
.lp-list::-webkit-scrollbar-thumb {
  background: var(--lp-scrollbar-c);
  border-radius: 4px;
}

.lp-list-inner {
  position: relative;
  width: 100%;
}

.lp-row {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
}

.lp-measure {
  height: 0;
  overflow: hidden;
  visibility: hidden;
  pointer-events: none;
}
</style>
