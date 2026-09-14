/**
 * 单代理测速状态（模块级 Map：多个代理各自保留结果，跨路由不丢）。
 *
 * 测速是外部重操作（Cloudflare 节点，单个约 10s 且吃带宽），只手动触发——面板
 * 不做后台轮询（用户明确要求）。结果就地展示在行下质量带里。
 *
 * 状态对齐：后端（PanelTasks）保存测速任务状态（running/done/error + 结果 + 时间戳），
 * /api/status 每行下发 speedtest 字段。渲染用 speedTestEntryOf 以**后端状态优先**——
 * 刷新页面后据此恢复"测速中/结果"；本地 Map 仅在无后端记录时兜底（请求刚发出
 * 的乐观显示 / 旧会话跨路由保留）。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import type { SpeedTestResult, SpeedTestStatus } from '@/types/api';

export interface SpeedTestState {
  status: 'running' | 'done';
  result?: SpeedTestResult;
  error?: string;
  /** 完成时间（本地时钟，面板展示"什么时候测的"） */
  time?: string;
}

// 模块级状态：跨路由保留结果；只经 speedTestOf/runSpeedTest 读写（不导出可变更集合）
const speedTests = reactive(new Map<string, SpeedTestState>());

export function speedTestOf(bind: string): SpeedTestState | undefined {
  return speedTests.get(bind);
}

/** epoch 秒 → HH:MM:SS（后端完成时间戳与本地完成时刻统一格式） */
function fmtClock(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleTimeString('zh-CN', { hour12: false });
}

/**
 * 行内测速状态解析（纯函数，可单测）：后端状态权威，本地 Map 兜底。
 * - server running → 测速中（刷新后恢复；本地旧终态让位，后端正在新任务）
 * - server done/error → 终态（含结果/错误与后端完成时间）
 * - server 无记录 → 本地 Map（乐观/跨路由）
 */
export function speedTestEntryOf(
  bind: string,
  server: SpeedTestStatus | null | undefined,
): SpeedTestState | undefined {
  if (server?.status === 'running') return { status: 'running' };
  if (server?.status === 'done' || server?.status === 'error') {
    return {
      status: 'done',
      result: server.result ?? undefined,
      error: server.error ?? undefined,
      time: server.ts != null ? fmtClock(server.ts) : undefined,
    };
  }
  return speedTests.get(bind);
}

/** 触发测速（幂等：进行中直接返回）。失败就地展示错误文案，不弹 toast（行内可见） */
export async function runSpeedTest(bind: string): Promise<void> {
  if (speedTests.get(bind)?.status === 'running') return;
  speedTests.set(bind, { status: 'running' });
  try {
    const result = await api.speedTest(bind);
    if ('status' in result) {
      // 后端已有同 bind 测速任务在进行（刷新后重复点击/多标签页）：保持"测速中"，
      // 结果由后端任务完成后写状态，/api/status 轮询恢复
      speedTests.set(bind, { status: 'running' });
      return;
    }
    speedTests.set(bind, { status: 'done', result, time: fmtClock(Date.now() / 1000) });
  } catch (e) {
    speedTests.set(bind, {
      status: 'done',
      error: e instanceof Error ? e.message : String(e),
      time: fmtClock(Date.now() / 1000),
    });
  }
}
