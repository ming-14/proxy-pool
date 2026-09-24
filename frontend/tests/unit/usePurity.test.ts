/**
 * 纯净度状态解析与展示辅助纯函数单测：后端终态优先，本地 Map 兜底。
 * 覆盖 purityEntryOf 的 server 各分支、通用 usage 短标签与分数着色边界。
 */
import { describe, expect, it } from 'vitest';
import { purityEntryOf, purityScoreClass, usageTypeShort } from '@/composables/usePurity';

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
    const cached = { score: 35, usage_type: 'hosting' };
    expect(purityEntryOf('1.2.3.4:8080', null, cached)).toEqual({ status: 'done', ...cached });
  });

  it('checking 优先于旧缓存（用户刚触发，后端仍在检查）', () => {
    const cached = { score: 0, usage_type: 'residential' };
    expect(purityEntryOf('1.2.3.4:8080', { status: 'checking' }, cached)?.status).toBe('checking');
  });

  it('后端 error 优先于缓存结果（检查失败明确展示失败态）', () => {
    const cached = { score: 0, usage_type: 'residential' };
    expect(purityEntryOf('1.2.3.4:8080', { status: 'error', error: 'x' }, cached)?.status).toBe('error');
  });
});

describe('usageTypeShort', () => {
  it('归一化枚举全部有中文短标签', () => {
    const cases: Record<string, string> = {
      residential: '住宅',
      mobile: '移动网络',
      hosting: '机房',
      business: '企业',
      education: '学校',
      government: '政府',
      military: '军事',
      library: '图书馆',
      cdn: 'CDN',
      search_engine: '搜索引擎',
      reserved: '保留',
      unknown: '未知',
    };
    for (const [value, label] of Object.entries(cases)) {
      expect(usageTypeShort(value)).toBe(label);
    }
  });

  it('未知取值原样回显，空值显示 -', () => {
    expect(usageTypeShort('satellite')).toBe('satellite');
    expect(usageTypeShort('')).toBe('-');
  });
});

describe('purityScoreClass', () => {
  it('按分数分档着色（0 最干净，越高越脏）', () => {
    expect(purityScoreClass(0)).toBe('badge-success');
    expect(purityScoreClass(24)).toBe('badge-muted');
    expect(purityScoreClass(25)).toBe('badge-warning');
    expect(purityScoreClass(74)).toBe('badge-warning');
    expect(purityScoreClass(75)).toBe('badge-danger');
    expect(purityScoreClass(100)).toBe('badge-danger');
  });
});
