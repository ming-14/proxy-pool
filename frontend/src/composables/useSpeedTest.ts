/**
 * 单代理测速状态（模块级 Map：多个代理各自保留结果，跨路由不丢）。
 *
 * 测速是外部重操作（Cloudflare 节点，单个约 10s 且吃带宽），只手动触发——面板
 * 不做后台轮询（用户明确要求）。结果就地展示在行下质量带里。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import type { SpeedTestResult } from '@/types/api';

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

/** 触发测速（幂等：进行中直接返回）。失败就地展示错误文案，不弹 toast（行内可见） */
export async function runSpeedTest(bind: string): Promise<void> {
  if (speedTests.get(bind)?.status === 'running') return;
  speedTests.set(bind, { status: 'running' });
  try {
    const result = await api.speedTest(bind);
    speedTests.set(bind, { status: 'done', result, time: clockNow() });
  } catch (e) {
    speedTests.set(bind, {
      status: 'done',
      error: e instanceof Error ? e.message : String(e),
      time: clockNow(),
    });
  }
}

function clockNow(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}
