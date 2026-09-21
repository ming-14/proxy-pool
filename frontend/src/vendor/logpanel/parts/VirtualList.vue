<script setup lang="ts" generic="E extends object">
/**
 * 虚拟列表：窗口计算、行高实测与端部锚定交给 TanStack Virtual。
 *
 * - 端部锚定：anchorTo: 'end' —— 头部淘汰/末尾追加后按 key 锚定视口首行，滚动位置不漂移；
 * - 行高实测：measureElement 取 offsetHeight（未缩放布局单位），与滚动容器的滚动坐标同一坐标系；
 * - 贴底跟随：新条目到达与内容高度变化（含测高修正、面板由隐藏转为显示）都经同一个
 *   stickToEnd 收敛到列表底部；库内 followOnAppend 不可用，原因见 stickToEnd 注释。
 * 跟随意图由滚动手势维护：上滚即停、滚回底部即恢复。程序侧滚动只发生在跟随或锚定语义内
 * （跟随必落底部、锚定保持原位），因此不会把用户手动关闭的跟随顶回去。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useVirtualizer } from '@tanstack/vue-virtual';
import type { LogPanelHooks, LogPanelKey, LogPanelRules } from '../types';
import type { Matcher } from '../utils/filter';
import LogLine from './LogLine.vue';

/** 行高估算值（px）：未实测的行先按它排布，实测后自动修正 */
const ROW_ESTIMATE = 22;
/** 视口外多渲染的行数 */
const OVERSCAN = 4;
/** 贴底判定阈值（px） */
const END_THRESHOLD = 5;

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

const virtualizer = useVirtualizer(
  computed(() => {
    // 快照必须与 count 同步绑定：库在更新前后会用各自那轮 options 的 getItemKey 比对首尾 key，
    // 若这里读实时数据，旧 options 也会读到新 key，环形淘汰就被判成"没变化"而不做位置补偿
    const snapshot = source.value;
    return {
      count: snapshot.entries.length,
      getScrollElement: () => scrollEl.value,
      estimateSize: () => ROW_ESTIMATE,
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
  if (follow.value && !atEnd) follow.value = false;
  else if (!follow.value && atEnd) follow.value = true;
}

/**
 * 贴底跟随。不能用库的 followOnAppend：Vue 适配器在选项变更时立即调 _willUpdate，此刻本框架
 * 尚未提交 DOM（撑高块还是旧高度），按 'end' 算出的目标偏移是过期的，写回反而把视口拉到上面。
 * 因此等本次提交完成后再算。
 */
async function stickToEnd(): Promise<void> {
  await nextTick();
  if (follow.value) virtualizer.value.scrollToEnd();
}

watch(() => props.entries, stickToEnd);

/**
 * 内容高度变化时也要贴底：新条目之外，行高实测修正（估算 → 实测）会撑高内容，
 * 面板由隐藏转为显示时撑高块高度也从 0 恢复，都走同一条贴底路径。
 */
let sizerObserver: ResizeObserver | null = null;
onMounted(() => {
  if (!sizerEl.value) return;
  sizerObserver = new ResizeObserver(stickToEnd);
  sizerObserver.observe(sizerEl.value);
});
onBeforeUnmount(() => sizerObserver?.disconnect());

defineExpose({
  /** 窗口尺寸变化后重测行高（宽度变了，换行行数随之变） */
  remeasure: () => virtualizer.value.measure(),
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
</style>