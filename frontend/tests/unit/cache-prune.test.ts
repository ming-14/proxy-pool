/**
 * 模块级缓存的淘汰：离队代理的条目必须被丢弃（否则随驻留时长无界增长）。
 *
 * 三个缓存（纯净度 / 测速 / 归属地）跨路由保留，是本页交互所需；但它们只按 bind
 * 累积、命中后永不再查，缺少淘汰就是内存泄漏。这里锁住"保留在场、丢弃离队"的语义，
 * 以及"不在场集合为空时不动手"（队列暂时为空不应误清）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

// api 客户端整体 mock：这些用例只关心本地缓存的增删，不涉及网络
vi.mock('@/api/client', () => ({
  api: {
    standbyGeo: vi.fn(),
    purity: vi.fn(),
    speedTest: vi.fn(),
  },
  errText: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import { purityCache, prunePurityCache } from '@/composables/usePurity';
import { pruneSpeedTests } from '@/composables/useSpeedTest';
import { pruneStandbyGeo, refreshStandbyGeo, standbyGeo } from '@/composables/useStandbyGeo';
import { api } from '@/api/client';

beforeEach(() => {
  purityCache.clear();
  standbyGeo.clear();
});

describe('prunePurityCache', () => {
  it('保留在场代理的条目', () => {
    purityCache.set('1.1.1.1:80', { status: 'done', score: 10 } as never);
    prunePurityCache(['1.1.1.1:80']);
    expect(purityCache.has('1.1.1.1:80')).toBe(true);
  });

  it('丢弃离队代理的条目', () => {
    purityCache.set('1.1.1.1:80', { status: 'done', score: 10 } as never);
    purityCache.set('2.2.2.2:80', { status: 'done', score: 20 } as never);
    prunePurityCache(['1.1.1.1:80']);
    expect(purityCache.has('1.1.1.1:80')).toBe(true);
    expect(purityCache.has('2.2.2.2:80')).toBe(false);
  });

  it('反复入队/离队不会累积', () => {
    for (let i = 0; i < 50; i++) {
      purityCache.set(`10.0.0.${i}:80`, { status: 'done', score: i } as never);
      prunePurityCache([`10.0.0.${i}:80`]);
    }
    expect(purityCache.size).toBe(1);
  });
});

describe('pruneStandbyGeo', () => {
  it('丢弃离队代理的归属地', () => {
    standbyGeo.set('1.1.1.1:80', { country: '中国' } as never);
    standbyGeo.set('2.2.2.2:80', { country: '美国' } as never);
    pruneStandbyGeo(['1.1.1.1:80']);
    expect(standbyGeo.has('1.1.1.1:80')).toBe(true);
    expect(standbyGeo.has('2.2.2.2:80')).toBe(false);
  });

  /**
   * 回归：进入队列页首帧 binds 必为空（displayStandby 尚未填充），此时刷新若照常清理，
   * 会清空整个归属地缓存；又因本函数有 5 分钟节流，所有行的归属地列要空窗到下一拍。
   */
  it('刷新时保留集为空则不清缓存（首帧不误清）', async () => {
    vi.mocked(api.standbyGeo).mockResolvedValue({ geo: {} } as never);
    standbyGeo.set('1.1.1.1:80', { country: '中国' } as never);
    await refreshStandbyGeo(true, []); // 首帧：队列尚空
    expect(standbyGeo.has('1.1.1.1:80')).toBe(true);
  });
});

describe('pruneSpeedTests', () => {
  it('丢弃离队代理的测速结果（不抛错、幂等）', () => {
    expect(() => pruneSpeedTests(['1.1.1.1:80'])).not.toThrow();
    expect(() => pruneSpeedTests(['1.1.1.1:80'])).not.toThrow();
  });
});
