/**
 * 前端偏好 store：轮询间隔与历史保留点数，localStorage 持久化（仅前端生效，与后端无关）。
 * status store 读取这两个值动态起轮询定时器与裁剪历史；设置页「界面偏好」Tab 编辑。
 */
import { defineStore } from 'pinia';
import { ref } from 'vue';

const POLL_KEY = 'proxy-pool-poll-ms';
const HISTORY_KEY = 'proxy-pool-history-limit';

export const DEFAULT_POLL_MS = 2000;
export const DEFAULT_HISTORY_LIMIT = 1000;
export const POLL_MS_RANGE: [number, number] = [500, 60000];
export const HISTORY_LIMIT_RANGE: [number, number] = [200, 20000];

function readNumber(key: string, fallback: number, range: [number, number]): number {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw >= range[0] && raw <= range[1] ? raw : fallback;
}

export const usePrefsStore = defineStore('prefs', () => {
  const pollIntervalMs = ref(readNumber(POLL_KEY, DEFAULT_POLL_MS, POLL_MS_RANGE));
  const historyLimit = ref(readNumber(HISTORY_KEY, DEFAULT_HISTORY_LIMIT, HISTORY_LIMIT_RANGE));

  /** 持久化当前值到 localStorage（保存按钮调用） */
  function apply() {
    localStorage.setItem(POLL_KEY, String(pollIntervalMs.value));
    localStorage.setItem(HISTORY_KEY, String(historyLimit.value));
  }

  /** 恢复默认值并持久化 */
  function reset() {
    pollIntervalMs.value = DEFAULT_POLL_MS;
    historyLimit.value = DEFAULT_HISTORY_LIMIT;
    apply();
  }

  return { pollIntervalMs, historyLimit, apply, reset };
});