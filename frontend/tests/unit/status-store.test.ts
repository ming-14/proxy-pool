/** status store：轮询成功/失败连接态、历史合并、暂停行为（mock fetch） */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useStatusStore } from '@/stores/status';

function mockResponses(status: unknown, history: unknown) {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const body = String(path).includes('/api/status/history') ? history : status;
    return { ok: true, status: 200, json: async () => body };
  }));
}

/** 模拟页面可见性（jsdom 的 document.hidden 恒为 false，用自有属性改写） */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.unstubAllGlobals();
  setHidden(false); // 可见性可能被上一个用例改写，逐例归位
});

describe('useStatusStore.tick', () => {
  it('成功时写入快照并置在线，历史按开关拉取', async () => {
    mockResponses({ groups: {}, standby: [] }, { standby_history: [{ ts: 1 }], traffic_history: [], retention_window: 1800 });
    const store = useStatusStore();
    store.historyEnabled = true;
    await store.tick();
    expect(store.connected).toBe(true);
    expect(store.status).toBeTruthy();
    expect(store.standbyHistory).toEqual([{ ts: 1 }]);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('historyEnabled 关闭时不拉历史', async () => {
    mockResponses({}, {});
    const store = useStatusStore();
    await store.tick();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('status 请求失败置离线但不抛异常', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('net down');
    }));
    const store = useStatusStore();
    await store.tick();
    expect(store.connected).toBe(false);
  });

  it('paused 时 tick 为空操作', async () => {
    mockResponses({}, {});
    const store = useStatusStore();
    store.paused = true;
    await store.tick();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('页面切后台时 tick 为空操作，切回前台立即补一次', async () => {
    mockResponses({ standby: [{ proxy: 'a' }] }, {});
    const store = useStatusStore();
    setHidden(true);
    await store.tick();
    expect(fetch).not.toHaveBeenCalled();
    expect(store.connected).toBe(false);
    setHidden(false);
    await store.tick();
    expect(store.connected).toBe(true);
  });

  it('start 注册可见性监听：切回前台不等下一拍就取数', async () => {
    vi.useFakeTimers();
    const store = useStatusStore();
    try {
      mockResponses({ standby: [] }, {});
      store.start(); // 首帧立即拉一次
      expect(fetch).toHaveBeenCalledTimes(1);
      setHidden(false);
      document.dispatchEvent(new Event('visibilitychange'));
      expect(fetch).toHaveBeenCalledTimes(2);
      store.stop();
      document.dispatchEvent(new Event('visibilitychange'));
      expect(fetch).toHaveBeenCalledTimes(2); // stop 已摘掉监听
    } finally {
      store.stop(); // 断言失败时也不留 interval
      vi.useRealTimers();
    }
  });

  it('增量游标：第二轮历史请求带 since=最后 ts', async () => {
    mockResponses({}, { standby_history: [{ ts: 5 }], traffic_history: [], retention_window: 1800 });
    const store = useStatusStore();
    store.historyEnabled = true;
    await store.tick(); // calls: status, history(无 since)
    await store.tick(); // calls: status, history(since=5)
    const historyCall = vi.mocked(fetch).mock.calls[3][0] as string;
    expect(historyCall).toContain('since=5');
  });
});

describe('useStatusStore.refreshNow', () => {
  it('paused 时仍取数（写动作后的即时刷新不受暂停影响）', async () => {
    mockResponses({ standby: [{ proxy: 'a' }] }, {});
    const store = useStatusStore();
    store.paused = true;
    await store.tick();
    expect(fetch).not.toHaveBeenCalled();
    await store.refreshNow();
    expect(store.status).toEqual({ standby: [{ proxy: 'a' }] });
    expect(store.connected).toBe(true);
  });
});
