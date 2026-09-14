/**
 * 测速状态解析纯函数单测：后端状态权威，本地 Map 兜底。
 * 覆盖 speedTestEntryOf 的 server 各分支（本地 Map 为模块私有，无记录即 undefined）。
 */
import { describe, expect, it } from 'vitest';
import { speedTestEntryOf } from '@/composables/useSpeedTest';

describe('speedTestEntryOf', () => {
  it('无后端记录返回 undefined（本地 Map 无条目）', () => {
    expect(speedTestEntryOf('1.2.3.4:8080', null)).toBeUndefined();
    expect(speedTestEntryOf('1.2.3.4:8080', undefined)).toBeUndefined();
  });

  it('后端 running → 测速中（刷新后恢复）', () => {
    expect(speedTestEntryOf('1.2.3.4:8080', { status: 'running' })).toEqual({ status: 'running' });
  });

  it('后端 done → 结果 + 后端完成时间', () => {
    const entry = speedTestEntryOf('1.2.3.4:8080', {
      status: 'done',
      result: { proxy: 'http://1.2.3.4:8080', download: null, upload: null },
      ts: 1700000000,
    });
    expect(entry?.status).toBe('done');
    expect(entry?.result?.proxy).toBe('http://1.2.3.4:8080');
    expect(entry?.time).toMatch(/^\d{2}:\d{2}:\d{2}$/); // HH:MM:SS
  });

  it('后端 error → 失败文案（前端展示"测速失败"）', () => {
    const entry = speedTestEntryOf('1.2.3.4:8080', { status: 'error', error: 'boom', ts: 1700000000 });
    expect(entry?.status).toBe('done');
    expect(entry?.error).toBe('boom');
  });

  it('后端 running 时本地旧终态让位（后端正在新任务）', () => {
    expect(speedTestEntryOf('1.2.3.4:8080', { status: 'running' })?.status).toBe('running');
  });
});
