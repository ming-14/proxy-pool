<script setup lang="ts">
/**
 * LogPanel 探针应用：以合成日志源驱动组件，并把驱动器挂到 window.__probe 供 Playwright 断言。
 *
 * 合成数据走固定种子 LCG，行高分布贴近真实面板（多数单行、少量多行、极少数带堆栈）。
 * 驱动器只读组件渲染出的 DOM（状态文本、滚动几何、行元素、按钮 aria-pressed），不触碰组件
 * 内部字段——断言只依赖用户可见行为，组件实现变化不会让用例失效。
 */
import { ref } from 'vue';
import { LogPanel } from '@/vendor/logpanel';
import { LONG_PRESS_MS, STORAGE_KEYS } from '@/vendor/logpanel/defaults';
import type { LogPanelEntry, LogPanelForm, LogPanelRules, LogPanelSource } from '@/vendor/logpanel';

/** 持久化前缀：与模板传给组件的保持一致，几何持久化断言按它读 */
const STORAGE_PREFIX = 'probe-logpanel_';

/** 探针条目：默认访问器要求的形状 + 单调 seq（用作 rules.key） */
interface ProbeEntry extends LogPanelEntry {
  seq: number;
  levelName: string;
  tag: string;
  text: string;
  stack: string | null;
}

interface ProbeState {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  distanceFromEnd: number;
  domLines: number;
  topGap: number | null;
  bottomGap: number | null;
  shown: number;
  buffered: number;
  paused: boolean;
  follow: boolean;
}

/** 缓冲容量，与后端 logbuffer 保持一致 */
const CAPACITY = 2000;
const TAGS = [
  'use_cases.fetch_proxies',
  'interfaces.proxy_server',
  'framework.web.api',
  'entities.proxy',
  'infra.geolocation',
  'scheduler',
];
const GEOMETRY = { x: 40, y: 40, w: 720, h: 480 };

let entries: ProbeEntry[] = [];
let listener: ((item: ProbeEntry | { type: 'clear' }) => void) | null = null;
let seq = 0;
let seed = 20260922;

/** 固定种子 LCG：数据分布可重复，断言才稳定 */
function random(): number {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
}

function makeEntry(): ProbeEntry {
  seq += 1;
  const kind = random();
  let text: string;
  if (kind < 0.75) {
    text = `心跳正常 延时 ${20 + (seq % 80)}ms`;
  } else if (kind < 0.95) {
    text = `校验通过 10.0.${seq % 255}.${seq % 99}:8080 延时 ${seq % 300}ms 已入组`;
  } else {
    text = `拉取批次 #${seq} 完成，共 ${seq % 50} 个代理，其中 ${seq % 7} 个校验失败已丢弃，耗时 ${seq % 900}ms`;
  }
  const error = kind >= 0.95 && seq % 3 === 0;
  return {
    seq,
    ts: Date.now(),
    level: error ? 3 : kind >= 0.95 ? 2 : 1,
    levelName: error ? 'ERROR' : kind >= 0.95 ? 'WARN' : 'INFO',
    tag: TAGS[seq % TAGS.length],
    text,
    stack: error
      ? `Traceback (most recent call last):\n  File "x.py", line ${seq}, in run\n    raise RuntimeError("boom")`
      : null,
  };
}

const source: LogPanelSource<ProbeEntry> = {
  subscribe(handler) {
    listener = handler;
    return () => {
      listener = null;
    };
  },
  getEntries: () => entries,
  clear() {
    entries = [];
    listener?.({ type: 'clear' });
  },
  getSize: () => entries.length,
  getCapacity: () => CAPACITY,
};

const probeRules: Partial<LogPanelRules<ProbeEntry>> = { key: (entry) => entry.seq };
const shown = ref(true);
const formMode = ref<LogPanelForm>('auto');

// ---- 驱动器：只经 DOM 读状态、经真实交互改状态 ----
const rootEl = () => document.querySelector<HTMLElement>('[data-testid="logpanel"]');
const listEl = () => rootEl()?.querySelector<HTMLElement>('.lp-list') ?? null;
// 只认渲染出来的行：.lp-measure 里的测高池行不是列表内容
const lineEls = () => Array.from(rootEl()?.querySelectorAll<HTMLElement>('.lp-row .lp-log-line') ?? []);

function pressed(testId: string): boolean {
  return rootEl()?.querySelector(`[data-testid="${testId}"]`)?.getAttribute('aria-pressed') === 'true';
}

function click(testId: string): void {
  rootEl()?.querySelector<HTMLElement>(`[data-testid="${testId}"]`)?.click();
}

function push(count: number): number {
  const batch: ProbeEntry[] = [];
  for (let i = 0; i < count; i++) batch.push(makeEntry());
  entries = entries.concat(batch);
  if (entries.length > CAPACITY) entries = entries.slice(-CAPACITY);
  for (const entry of batch) listener?.(entry); // 真实链路是逐条回调
  return entries.length;
}

function state(): ProbeState {
  const list = listEl();
  const rows = lineEls();
  const listRect = list?.getBoundingClientRect();
  const [shownCount, bufferedCount] = (rootEl()?.querySelector('.lp-status')?.textContent ?? '')
    .split('/')
    .map((part) => Number.parseInt(part.trim(), 10) || 0);
  const scrollTop = list?.scrollTop ?? 0;
  const scrollHeight = list?.scrollHeight ?? 0;
  const clientHeight = list?.clientHeight ?? 0;
  return {
    scrollTop,
    scrollHeight,
    clientHeight,
    distanceFromEnd: Math.max(0, scrollHeight - scrollTop - clientHeight),
    domLines: rows.length,
    topGap: listRect && rows.length ? Number((rows[0].getBoundingClientRect().top - listRect.top).toFixed(1)) : null,
    bottomGap:
      listRect && rows.length
        ? Number((listRect.bottom - rows[rows.length - 1].getBoundingClientRect().bottom).toFixed(1))
        : null,
    shown: shownCount,
    buffered: bufferedCount,
    paused: pressed('lp-pause'),
    follow: pressed('lp-autoscroll'),
  };
}

function scrollTo(offset: number): void {
  const list = listEl();
  if (!list) return;
  list.scrollTop = offset;
  list.dispatchEvent(new Event('scroll')); // 同步触发，省去等待原生滚动事件
}

function lineTextAt(index: number): string | null {
  return lineEls()[index]?.querySelector('.lp-text')?.textContent ?? null;
}

function lineTop(index: number): number | null {
  const list = listEl();
  const line = lineEls()[index];
  if (!list || !line) return null;
  return Number((line.getBoundingClientRect().top - list.getBoundingClientRect().top).toFixed(1));
}

/** 视口内第一个可展开行（带堆栈）的下标；没有则 -1 */
function firstExpandable(): number {
  return lineEls().findIndex((el) => el.classList.contains('has-stack'));
}

/** 当前渲染出的展开行正文（按条目查展开态，避免依赖行下标） */
function expandedTexts(): string[] {
  return lineEls()
    .filter((el) => el.classList.contains('expanded'))
    .map((el) => el.querySelector('.lp-text')?.textContent ?? '');
}

function setQuery(value: string): void {
  const input = rootEl()?.querySelector<HTMLInputElement>('[data-testid="lp-search"]');
  if (!input) return;
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true })); // 走 v-model 的真实输入路径
}

/** 在标题栏上派发右键事件（真实 contextmenu 路径），并回报菜单是否出现 */
function openMenu(): boolean {
  const bar = rootEl()?.querySelector('.lp-titlebar');
  if (!bar) return false;
  bar.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 300, clientY: 200 }));
  return menuVisible();
}

function menuVisible(): boolean {
  return rootEl()?.querySelector('.lp-menu') !== null;
}

function clickMenuClose(): void {
  rootEl()?.querySelector<HTMLElement>('[data-testid="lp-menu-close"]')?.click();
}

// ---- 形态与抽屉驱动 ----
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** 合成 pointer 事件：走组件真实手势路径（move/up 监听在 window 上） */
function pointerEvent(type: string, x: number, y: number, pointerType: string): PointerEvent {
  return new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    composed: true,
    pointerId: 1,
    pointerType,
    isPrimary: true,
    button: 0,
    buttons: type === 'pointerup' ? 0 : 1,
    clientX: x,
    clientY: y,
  });
}

function titleBar(): HTMLElement | null {
  return rootEl()?.querySelector<HTMLElement>('.lp-titlebar') ?? null;
}

/** 标题栏上的落点：空白处（避开按钮） */
function titlePoint(): { x: number; y: number } | null {
  const rect = titleBar()?.getBoundingClientRect();
  return rect ? { x: rect.left + 40, y: rect.top + rect.height / 2 } : null;
}

// 手势按起手点算：拖拽中面板自身会移动，不能每步重新量标题栏
let gestureAt: { x: number; y: number } | null = null;
let gestureDy = 0;

function titleDown(pointerType = 'mouse'): void {
  const bar = titleBar();
  const at = titlePoint();
  if (!bar || !at) return;
  gestureAt = at;
  gestureDy = 0;
  bar.dispatchEvent(pointerEvent('pointerdown', at.x, at.y, pointerType));
}

/** 拖到相对起手点 dy 处；durationMs 决定速度（快滑按速度翻档，慢拖按位移定档） */
async function titleDragTo(dy: number, durationMs: number, pointerType = 'mouse'): Promise<void> {
  if (!gestureAt) return;
  const at = gestureAt;
  gestureDy = dy;
  const steps = 8;
  for (let step = 1; step <= steps; step++) {
    await sleep(durationMs / steps);
    window.dispatchEvent(pointerEvent('pointermove', at.x, at.y + (dy * step) / steps, pointerType));
  }
}

function titleUp(): void {
  if (!gestureAt) return;
  window.dispatchEvent(pointerEvent('pointerup', gestureAt.x, gestureAt.y + gestureDy, 'mouse'));
  gestureAt = null;
}

async function dragTitle(dy: number, durationMs = 400, pointerType = 'mouse'): Promise<void> {
  titleDown(pointerType);
  await titleDragTo(dy, durationMs, pointerType);
  titleUp();
}

/** 拖到 dy 处但不松手：用于读取"松手前"的视窗位置 */
async function dragTitleHold(dy: number, durationMs = 400): Promise<void> {
  titleDown();
  await titleDragTo(dy, durationMs);
}

/** 标题栏点按（抽屉态切换档位） */
async function tapTitle(): Promise<void> {
  titleDown('touch');
  titleUp();
}

/** 长按标题栏：等过长按阈值再抬手（触屏打开菜单的路径） */
async function longPressTitle(): Promise<void> {
  titleDown('touch');
  await sleep(LONG_PRESS_MS + 150);
  titleUp();
}

function formOf(): string | null {
  return rootEl()?.getAttribute('data-form') ?? null;
}

function detentOf(): string | null {
  return rootEl()?.getAttribute('data-detent') ?? null;
}

/** 根节点盒模型（视口坐标）：抽屉满宽贴底断言用 */
function rootBox(): { x: number; y: number; w: number; h: number; bottom: number } | null {
  const rect = rootEl()?.getBoundingClientRect();
  return rect ? { x: rect.x, y: rect.y, w: rect.width, h: rect.height, bottom: rect.bottom } : null;
}

function listClientHeight(): number {
  return listEl()?.clientHeight ?? 0;
}

/** 模块筛选条是否展开（读开关按钮的 aria-expanded） */
function sideOpen(): boolean {
  return rootEl()?.querySelector('[data-testid="lp-side-toggle"]')?.getAttribute('aria-expanded') === 'true';
}

/** 根节点内联样式里出现的属性名：形态切换后不得残留上一形态的键 */
function inlineStyleKeys(): string[] {
  return (rootEl()?.getAttribute('style') ?? '')
    .split(';')
    .map((declaration) => declaration.split(':')[0]?.trim() ?? '')
    .filter(Boolean);
}

function geomPersisted(): unknown {
  return JSON.parse(localStorage.getItem(`${STORAGE_PREFIX}${STORAGE_KEYS.geometry}`) ?? 'null');
}

Object.assign(window, {
  __ready: true,
  __probe: {
    push,
    state,
    scrollTo,
    lineTextAt,
    lineTop,
    firstExpandable,
    expandedTexts,
    hide: () => (shown.value = false),
    show: () => (shown.value = true),
    clickPause: () => click('lp-pause'),
    clickAutoScroll: () => click('lp-autoscroll'),
    clickClear: () => click('lp-clear'),
    clickLine: (index: number) => lineEls()[index]?.click(),
    setQuery,
    openMenu,
    menuVisible,
    clickMenuClose,
    toggleRegex: () => click('lp-regex'),
    toggleLevel: (level: number) => click(`lp-level-${level}`),
    toggleTag: (tag: string) => rootEl()?.querySelector<HTMLElement>(`[data-tag="${tag}"]`)?.click(),
    setForm: (mode: LogPanelForm) => (formMode.value = mode),
    formOf,
    detentOf,
    rootBox,
    listClientHeight,
    sideOpen,
    toggleSide: () => click('lp-side-toggle'),
    clickDetent: () => click('lp-detent'),
    tapTitle,
    dragTitle,
    dragTitleHold,
    releaseTitle: titleUp,
    longPressTitle,
    inlineStyleKeys,
    geomPersisted,
  },
});
</script>

<template>
  <LogPanel
    v-model:visible="shown"
    :source="source"
    :rules="probeRules"
    :form="formMode"
    theme="dark"
    title="日志面板探针"
    :storage-key-prefix="STORAGE_PREFIX"
    :initial-geometry="GEOMETRY"
  />
</template>