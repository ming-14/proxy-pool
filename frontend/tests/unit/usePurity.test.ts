/**
 * 纯洁度状态解析纯函数单测：后端终态优先，本地 Map 兜底。
 * 覆盖 purityEntryOf 的 server 各分支与缓存结果（本地 Map 为模块私有，无记录即 null）。
 */
import { describe, expect, it } from 'vitest';
import { purityEntryOf } from '@/composables/usePurity';

describe('purityEntryOf', () => {
  it('无后端记录且无缓存返回 null（本地 Map 无条目）', () => {
    expect(purityEntryOf('1.2.3.4:8080', null, null)).toBeNull();
  });

  it('后端 checking → 检查中（刷新后恢复）', () => {
    expect(purityEntryOf('1.2.3.4:8080', { status: 'checking' }, null)).toEqual({ status: 'checking' });
  });

  it('后端 error → 检查失败', () => {
    expect(purityEntryOf('1.2.3.4:8080', { status: 'error', error: 'API down' }, null)).toEqual({
      status: 'error',
      error: 'API down',
    });
    expect(purityEntryOf('1.2.3.4:8080', { status: 'error' }, null)?.error).toBe('检查失败');
  });

  it('缓存结果（row.purity）→ 已完成结果', () => {
    const cached = { abuse_confidence_score: 35, usage_type: 'hosting' };
    expect(purityEntryOf('1.2.3.4:8080', null, cached)).toEqual({ status: 'done', ...cached });
  });

  it('checking 优先于旧缓存（用户刚触发，后端仍在检查）', () => {
    const cached = { abuse_confidence_score: 0, usage_type: 'residential' };
    expect(purityEntryOf('1.2.3.4:8080', { status: 'checking' }, cached)?.status).toBe('checking');
  });

  it('后端 error 优先于缓存结果（检查失败明确展示失败态）', () => {
    const cached = { abuse_confidence_score: 0, usage_type: 'residential' };
    expect(purityEntryOf('1.2.3.4:8080', { status: 'error', error: 'x' }, cached)?.status).toBe('error');
  });
});
