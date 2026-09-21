/**
 * 日志视图状态机：订阅源的增量信号、按帧重读缓冲快照，派生过滤视图、模块列表与统计。
 *
 * 快照与源分离——源是宿主自己的环形缓冲，组件只在 flush 时读一次，避免逐条渲染；
 * 过滤/统计全部由 computed 派生，不再手工重建视图。暂停只冻结快照（含统计），
 * 恢复时一次性补齐暂停期间的条目。
 */
import { computed, onBeforeUnmount, reactive, ref, shallowRef, triggerRef, watch } from 'vue';
import { STORAGE_KEYS } from '../defaults';
import type {
  LogPanelClear,
  LogPanelHooks,
  LogPanelKey,
  LogPanelRules,
  LogPanelSource,
} from '../types';
import { accepts, collectTags, countLevels, createMatcher } from '../utils/filter';
import { formatEntryPlain } from '../utils/format';
import type { PanelStorage } from '../utils/storage';

export interface LogViewOptions<E> {
  source: LogPanelSource<E>;
  rules: LogPanelRules<E>;
  hooks?: LogPanelHooks<E>;
  storage: PanelStorage;
}

export function useLogView<E extends object>(options: LogViewOptions<E>) {
  const { source, rules, hooks, storage } = options;

  function readList<T>(key: string, fallback: T[]): T[] {
    const raw = storage.readJSON<unknown>(key, fallback);
    return Array.isArray(raw) ? (raw as T[]) : fallback;
  }

  const entries = shallowRef<E[]>([]);
  const paused = ref(false);
  const query = ref('');
  const useRegex = ref(false);
  const levels = reactive(new Set<number>(readList(STORAGE_KEYS.levels, [0, 1, 2, 3])));
  const hiddenTags = reactive(new Set<string>(readList(STORAGE_KEYS.hiddenTags, [])));
  const expandedKeys = reactive(new Set<LogPanelKey>());

  /** 条目标识：rules.key 优先，未提供时按对象引用分配稳定 id */
  const identityKeys = new WeakMap<object, number>();
  let nextIdentityKey = 1;

  function keyOf(entry: E): LogPanelKey {
    if (rules.key) return rules.key(entry);
    let key = identityKeys.get(entry);
    if (key === undefined) {
      key = nextIdentityKey++;
      identityKeys.set(entry, key);
    }
    return key;
  }

  const matcher = computed(() => createMatcher(query.value, useRegex.value));

  /** hooks.onEntry 变换后的条目；返回 null 视为丢弃 */
  const mapped = computed<E[]>(() => {
    if (!hooks?.onEntry) return entries.value;
    const result: E[] = [];
    for (const entry of entries.value) {
      try {
        const next = hooks.onEntry(entry);
        if (next) result.push(next);
      } catch (error) {
        console.error('[logpanel] onEntry 钩子抛出异常，该条丢弃', error);
      }
    }
    return result;
  });

  const view = computed(() =>
    mapped.value.filter((entry) =>
      accepts(entry, rules, hooks, { levels, hiddenTags, matcher: matcher.value }),
    ),
  );
  const tags = computed(() => collectTags(mapped.value, rules));
  const levelCounts = computed(() => countLevels(mapped.value, rules));

  /** 缓冲条数：源未提供 getSize 时退回快照长度（读 mapped 以跟随 flush 重算） */
  const bufferSize = computed(() => {
    const fallback = mapped.value.length;
    try {
      return source.getSize?.() ?? fallback;
    } catch (error) {
      console.error('[logpanel] source.getSize 抛出异常', error);
      return fallback;
    }
  });

  /** 缓冲容量：源未提供时返回 0，面板据此隐藏占比 */
  const bufferCapacity = computed(() => {
    try {
      return source.getCapacity?.() ?? 0;
    } catch (error) {
      console.error('[logpanel] source.getCapacity 抛出异常', error);
      return 0;
    }
  });

  let frame = 0;

  function refresh(): void {
    try {
      entries.value = source.getEntries() ?? [];
    } catch (error) {
      console.error('[logpanel] source.getEntries 抛出异常', error);
      entries.value = [];
    }
    triggerRef(entries); // 宿主原地复用同一数组时也要触发派生重算
  }

  /** 一帧内的多条增量合并成一次快照重读；暂停期间不刷新 */
  function schedule(): void {
    if (frame !== 0 || paused.value) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      refresh();
    });
  }

  const unsubscribe = source.subscribe((item) => {
    if ((item as Partial<LogPanelClear>).type === 'clear') {
      expandedKeys.clear();
      refresh();
      return;
    }
    schedule();
  });

  function togglePause(): void {
    paused.value = !paused.value;
    if (!paused.value) refresh();
  }

  function toggleLevel(level: number): void {
    if (levels.has(level)) levels.delete(level);
    else levels.add(level);
  }

  function toggleTag(tag: string): void {
    if (hiddenTags.has(tag)) hiddenTags.delete(tag);
    else hiddenTags.add(tag);
  }

  /** 全选 ↔ 取消全选：已全部隐藏则全部显示，否则全部隐藏 */
  function toggleAllTags(): void {
    if (tags.value.length > 0 && tags.value.every((tag) => hiddenTags.has(tag))) {
      hiddenTags.clear();
    } else {
      for (const tag of tags.value) hiddenTags.add(tag);
    }
  }

  function toggleExpanded(key: LogPanelKey): void {
    if (expandedKeys.has(key)) expandedKeys.delete(key);
    else expandedKeys.add(key);
  }

  /** 清空缓冲：展开态由订阅回调统一丢弃（source.clear 须推送清空信号） */
  function clearBuffer(): void {
    source.clear?.();
  }

  /** 复制/导出的纯文本行（当前过滤结果） */
  function plainLines(): string[] {
    return view.value.map((entry) => formatEntryPlain(entry, rules, hooks));
  }

  watch(
    () => [...levels],
    (value) => storage.writeJSON(STORAGE_KEYS.levels, value),
  );
  watch(
    () => [...hiddenTags],
    (value) => storage.writeJSON(STORAGE_KEYS.hiddenTags, value),
  );

  refresh();
  onBeforeUnmount(() => {
    unsubscribe();
    if (frame !== 0) cancelAnimationFrame(frame);
  });

  return {
    paused,
    query,
    useRegex,
    levels,
    hiddenTags,
    expandedKeys,
    matcher,
    view,
    tags,
    levelCounts,
    bufferSize,
    bufferCapacity,
    keyOf,
    togglePause,
    toggleLevel,
    toggleTag,
    toggleAllTags,
    toggleExpanded,
    clearBuffer,
    plainLines,
  };
}