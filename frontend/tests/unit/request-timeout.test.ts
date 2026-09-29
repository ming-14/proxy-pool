/**
 * 请求超时与轮询防重入。
 *
 * 背景：面板按固定节拍（2s）轮询，后端若僵死（连上不回包），未设超时的 fetch 会永远
 * 挂着、每轮再叠一个，且永远走不到失败分支。这里锁两件事：
 * 1. request 带超时，挂起的请求会以 AbortError 结束；
 * 2. fetchStatus 的在途请求不会叠加——但 refreshNow（写动作后的即时刷新）必须
 *    等到最新数据，不能被在途请求直接挡掉。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const statusMock = vi.fn();
vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client');
  return {
    ...actual,
    api: {
      status: () => statusMock(),
      statusHistory: vi.fn(async () => ({ retention_window: 300, standby_history: [], traffic_history: [] })),
    },
  };
});

import { errText } from '@/api/client';

describe('errText', () => {
  it('Error 取 message，非 Error 兜底 String()', () => {
    expect(errText(new Error('boom'))).toBe('boom');
    expect(errText('plain')).toBe('plain');
    expect(errText(undefined)).toBe('undefined');
  });

  it('AbortError（超时）也能取到可读文案', () => {
    const e = new DOMException('signal timed out', 'TimeoutError');
    expect(errText(e)).toContain('timed out');
  });
});

describe('request 超时', () => {
  it('挂起的响应会在超时后抛错，不会永远等待', async () => {
    // 模拟真实 fetch 对 AbortSignal 的契约：信号 abort 时以 AbortError 拒绝
    let capturedSignal: AbortSignal | undefined;
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => {
      const signal = init?.signal;
      capturedSignal = signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
      });
    }));

    // 用独立模块实例（上面的 vi.mock 只作用于 store 用例的引用）
    const { api } = await vi.importActual<typeof import('@/api/client')>('@/api/client');
    const started = Date.now();
    const text: string = await api.status().then(
      () => '',
      (e: unknown) => errText(e),
    );
    const elapsed = Date.now() - started;

    // 关键：请求必须带 AbortSignal，且在超时（15s）内以错误收场而非永远挂起
    expect(capturedSignal).toBeInstanceOf(AbortSignal);
    expect(text.toLowerCase()).toContain('abort');
    expect(elapsed).toBeLessThan(20_000);

    vi.unstubAllGlobals();
  }, 30_000);
});

describe('状态轮询防重入', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    statusMock.mockReset();
  });

  it('在途期间的 tick 不叠加请求', async () => {
    const { useStatusStore } = await import('@/stores/status');
    const store = useStatusStore();

    let release!: () => void;
    statusMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () =>
            resolve({
              standby: [], groups: {}, pending_retries: [], geo_enabled: false,
              available_count: 0, standby_count: 0,
            });
        }),
    );

    const first = store.tick(); // 占住在途
    await Promise.resolve();
    await store.tick(); // 应被跳过
    await store.tick(); // 应被跳过
    expect(statusMock).toHaveBeenCalledTimes(1);

    statusMock.mockResolvedValue({
      standby: [], groups: {}, pending_retries: [], geo_enabled: false,
      available_count: 0, standby_count: 0,
    });
    release();
    await first;
  });

  it('晚到的旧轮询响应不覆盖 refreshNow 拿到的新数据', async () => {
    const { useStatusStore } = await import('@/stores/status');
    const store = useStatusStore();

    // 第一个请求（旧）挂起，稍后才返回"旧快照"
    let releaseOld!: () => void;
    statusMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseOld = () =>
            resolve({
              standby: [], groups: {}, pending_retries: [], geo_enabled: false,
              available_count: 1, standby_count: 0,
            });
        }),
    );
    const oldReq = store.tick();
    await Promise.resolve();

    // 此时写动作触发 refreshNow：拿到"新快照"
    statusMock.mockResolvedValue({
      standby: [], groups: {}, pending_retries: [], geo_enabled: false,
      available_count: 42, standby_count: 0,
    });
    await store.refreshNow();
    expect(store.status?.available_count).toBe(42);

    // 旧请求现在才返回：必须被丢弃，不能把界面打回 1
    releaseOld();
    await oldReq;
    expect(store.status?.available_count).toBe(42);
  });

  it('refreshNow 不被在途轮询挡掉，返回时拿到最新数据', async () => {
    const { useStatusStore } = await import('@/stores/status');
    const store = useStatusStore();

    let release!: () => void;
    statusMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () =>
            resolve({
              standby: [], groups: {}, pending_retries: [], geo_enabled: false,
              available_count: 1, standby_count: 0,
            });
        }),
    );
    const inflight = store.tick();
    await Promise.resolve();

    statusMock.mockResolvedValue({
      standby: [], groups: {}, pending_retries: [], geo_enabled: false,
      available_count: 42, standby_count: 0,
    });

    const refresh = store.refreshNow();
    release();
    await inflight;
    await refresh;

    // 写动作看重的是"返回后 status 已是最新"，故必须真的再取一次
    expect(statusMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(store.status?.available_count).toBe(42);
  });
});
