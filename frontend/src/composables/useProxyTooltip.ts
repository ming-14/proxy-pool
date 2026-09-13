/**
 * 代理 IP 统计浮窗状态机（模块级：跨路由卸载保留测速结果，与旧版语义一致）。
 *
 * 生命周期：悬停 300ms 显示 → 2s 自刷新历史；离开 100ms 销毁；点击 pin；
 * 测速中/完成自动 pin（仅点外部/✕关闭）；测速状态在浮窗重建后恢复。
 * 旧版"文本选区冻结重绘"补丁不再需要（Vue 不重建未变化的文本节点）。
 */
import { reactive, ref } from 'vue';
import { api } from '@/api/client';
import type { ProxyHistory, SpeedTestResult } from '@/types/api';

interface TooltipState {
  visible: boolean;
  bind: string;
  pinned: boolean;
  x: number;
  y: number;
}

export const tooltipState = reactive<TooltipState>({
  visible: false,
  bind: '',
  pinned: false,
  x: 0,
  y: 0,
});

export const tooltipHistory = ref<ProxyHistory | null>(null);

export interface SpeedTestState {
  status: 'running' | 'done';
  bind: string;
  result?: SpeedTestResult;
  error?: string;
  time?: string;
}

// 测速状态跨浮窗重建保留（隐藏后不丢结果）
export const speedTestState = ref<SpeedTestState | null>(null);

const SHOW_DELAY = 300;
const HIDE_DELAY = 100;
const REFRESH_MS = 2000;

let showTimer: ReturnType<typeof setTimeout> | null = null;
let hideTimer: ReturnType<typeof setTimeout> | null = null;
let refreshTimer: ReturnType<typeof setInterval> | null = null;

function stopRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

async function refreshHistory() {
  const bind = tooltipState.bind;
  try {
    const data = await api.proxyHistory(bind);
    if (tooltipState.bind === bind && tooltipState.visible) tooltipHistory.value = data;
  } catch {
    /* 浮窗数据刷新失败静默（下一轮重试），与旧版一致 */
  }
}

export function showTooltip(bind: string, x: number, y: number, force = false) {
  cancelShow();
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  if (tooltipState.visible && tooltipState.bind === bind) return;
  if (tooltipState.pinned && !force) return;
  hideTooltip();
  tooltipState.visible = true;
  tooltipState.bind = bind;
  tooltipState.x = x;
  tooltipState.y = y;
  refreshHistory();
  stopRefresh();
  refreshTimer = setInterval(refreshHistory, REFRESH_MS);
}

export function scheduleShow(bind: string, x: number, y: number) {
  cancelShow();
  showTimer = setTimeout(() => {
    showTimer = null;
    showTooltip(bind, x, y);
  }, SHOW_DELAY);
}

export function cancelShow() {
  if (showTimer) {
    clearTimeout(showTimer);
    showTimer = null;
  }
}

export function hideTooltip() {
  cancelShow();
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  stopRefresh();
  tooltipState.visible = false;
  tooltipState.pinned = false;
  tooltipState.bind = '';
  tooltipHistory.value = null;
}

export function scheduleHide() {
  if (tooltipState.pinned) return; // 测速/pin 状态不自动消失
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    hideTimer = null;
    hideTooltip();
  }, HIDE_DELAY);
}

/** 取消待执行的隐藏（鼠标移入浮窗本体时保持显示） */
export function cancelHide() {
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
}

export function pinTooltip() {
  tooltipState.pinned = true;
}

export async function runSpeedTest(bind: string) {
  speedTestState.value = { status: 'running', bind };
  pinTooltip();
  try {
    const result = await api.speedTest(bind);
    speedTestState.value = { status: 'done', bind, result, time: clockNow() };
  } catch (e) {
    speedTestState.value = {
      status: 'done',
      bind,
      error: e instanceof Error ? e.message : String(e),
      time: clockNow(),
    };
  }
}

function clockNow(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}
