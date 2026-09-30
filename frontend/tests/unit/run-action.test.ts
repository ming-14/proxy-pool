import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { runAction } from '@/composables/runAction';
import { useToastStore } from '@/stores/toast';

beforeEach(() => setActivePinia(createPinia()));

describe('runAction', () => {
  it('成功：按返回值生成 toast，after 执行，返回结果', async () => {
    const after = vi.fn();
    const result = await runAction(async () => ({ count: 3 }), {
      ok: (r) => ({ title: '已移动', message: `共 ${r.count} 个` }),
      failTitle: '移动失败',
      after,
    });
    expect(result).toEqual({ count: 3 });
    expect(after).toHaveBeenCalledOnce();
    expect(useToastStore().toasts[0]).toMatchObject({
      type: 'success',
      title: '已移动',
      message: '共 3 个',
    });
  });

  it('失败：error toast 取 errText，after 仍执行，返回 undefined', async () => {
    const after = vi.fn();
    const result = await runAction(
      async () => {
        throw new Error('boom');
      },
      { ok: { title: '已固定' }, failTitle: '固定失败', after },
    );
    expect(result).toBeUndefined();
    expect(after).toHaveBeenCalledOnce();
    expect(useToastStore().toasts[0]).toMatchObject({
      type: 'error',
      title: '固定失败',
      message: 'boom',
    });
  });

  it('after 抛错不外抛：收尾失败静默，不影响结果与成功 toast', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const result = await runAction(async () => ({ count: 1 }), {
      ok: { title: '已固定' },
      failTitle: '固定失败',
      after: async () => {
        throw new Error('refresh boom');
      },
    });
    expect(result).toEqual({ count: 1 });
    expect(useToastStore().toasts).toHaveLength(1);
    expect(useToastStore().toasts[0]).toMatchObject({ type: 'success', title: '已固定' });
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it('成功提示可改走 info（后端幂等拒绝）', async () => {
    await runAction(async () => ({ status: 'running' as const }), {
      ok: { type: 'info', title: '拉取进行中' },
      failTitle: '拉取失败',
    });
    expect(useToastStore().toasts[0]).toMatchObject({ type: 'info', title: '拉取进行中' });
  });
});
