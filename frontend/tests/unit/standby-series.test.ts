/** useStandbySeries / useStandbyGeo：游标增量、合并去重、窗口裁剪、离队丢弃（mock fetch） */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { geoText } from '@/composables/useStandbyGeo';

/** 服务端下发的队列图表显示窗口（秒）：与后端 chart_window 同源，前端不硬编码 */
const WINDOW_S = 1800;

/** 每个用例取全新模块实例（模块级缓冲/游标不跨用例串味） */
async function freshModule() {
  vi.resetModules();
  return await import('@/composables/useStandbySeries');
}

function mockSeries(body: unknown) {
  const payload = { window: WINDOW_S, ...(body as Record<string, unknown>) };
  const fetchMock = vi.fn(async (_path: string) => ({ ok: true, status: 200, json: async () => payload }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const nowS = () => Date.now() / 1000;

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe('refreshStandbySeries', () => {
  it('首次拉取不带 since，合并进缓冲并更新游标', async () => {
    const t = Math.round(nowS());
    const fetchMock = mockSeries({
      cursor: t + 2,
      series: { '1.2.3.4:8080': { lat: [[t, 0.2], [t + 2, null]], up: [[t, 10]], down: [[t, 20]] } },
    });
    const mod = await freshModule();
    await mod.refreshStandbySeries(['1.2.3.4:8080']);

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/standby/series');
    const buf = mod.seriesBuffers.get('1.2.3.4:8080')!;
    expect(buf.lat).toEqual([[t, 0.2], [t + 2, null]]);
    expect(buf.up).toEqual([[t, 10]]);
    // 第二次带上游标
    await mod.refreshStandbySeries(['1.2.3.4:8080']);
    expect(String(fetchMock.mock.calls[1][0])).toBe(`/api/standby/series?since=${t + 2}`);
  });

  it('增量点按时间序拼接，重复时间戳不重复累积', async () => {
    const t = Math.round(nowS());
    const mod = await freshModule();
    mockSeries({ cursor: t, series: { b: { lat: [[t, 0.2]] } } });
    await mod.refreshStandbySeries(['b']);
    mockSeries({ cursor: t + 2, series: { b: { lat: [[t, 0.2], [t + 2, 0.3]] } } });
    await mod.refreshStandbySeries(['b']);
    expect(mod.seriesBuffers.get('b')!.lat).toEqual([[t, 0.2], [t + 2, 0.3]]);
  });

  it('离队代理的缓冲被丢弃（不会无界增长）', async () => {
    const t = Math.round(nowS());
    const mod = await freshModule();
    mockSeries({ cursor: t, series: { gone: { lat: [[t, 0.1]] }, kept: { lat: [[t, 0.1]] } } });
    await mod.refreshStandbySeries(['gone', 'kept']);
    expect(mod.seriesBuffers.size).toBe(2);

    mockSeries({ cursor: t + 2, series: {} });
    await mod.refreshStandbySeries(['kept']);
    expect([...mod.seriesBuffers.keys()]).toEqual(['kept']);
  });

  it('窗口外的点被裁掉（缓冲只保留服务端下发窗口内的采样）', async () => {
    const mod = await freshModule();
    const stale = nowS() - WINDOW_S - 10;
    const fresh = nowS() - 5;
    mockSeries({
      cursor: fresh,
      series: { p: { lat: [[stale, 0.1], [fresh, 0.2]], up: [[stale, 1], [fresh, 2]] } },
    });
    await mod.refreshStandbySeries(['p']);
    const buf = mod.seriesBuffers.get('p')!;
    expect(buf.lat).toEqual([[fresh, 0.2]]);
    expect(buf.up).toEqual([[fresh, 2]]);
  });

  it('裁剪窗口取服务端下发的 window（前端不再硬编码一份，改配置即生效）', async () => {
    const mod = await freshModule();
    const now = nowS();
    mockSeries({
      cursor: now,
      window: 60,
      series: { p: { lat: [[now - 120, 0.1], [now - 5, 0.2]], up: [[now - 120, 1], [now - 5, 2]] } },
    });
    await mod.refreshStandbySeries(['p']);
    const buf = mod.seriesBuffers.get('p')!;
    expect(buf.lat).toEqual([[now - 5, 0.2]]);
    expect(buf.up).toEqual([[now - 5, 2]]);
  });

  it('缓冲全空时游标归零（下次全量拉取，避免只拿增量导致图表空转）', async () => {
    const t = Math.round(nowS());
    const mod = await freshModule();
    const fetchMock = mockSeries({ cursor: t, series: { a: { lat: [[t, 0.1]] } } });
    await mod.refreshStandbySeries(['a']);
    // 队列被清空（binds 为空）→ 缓冲清空 + 游标归零
    await mod.refreshStandbySeries([]);
    expect(mod.seriesBuffers.size).toBe(0);
    await mod.refreshStandbySeries(['a']);
    expect(String(fetchMock.mock.calls[2][0])).toBe('/api/standby/series');
  });

  it('请求失败静默（不抛异常、缓存保留），下一拍可重试', async () => {
    const t = Math.round(nowS());
    const mod = await freshModule();
    mockSeries({ cursor: t, series: { p: { lat: [[t, 0.1]] } } });
    await mod.refreshStandbySeries(['p']);

    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('net down');
    }));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(mod.refreshStandbySeries(['p'])).resolves.toBeUndefined();
    expect(mod.seriesBuffers.get('p')!.lat).toEqual([[t, 0.1]]);
    warn.mockRestore();

    mockSeries({ cursor: t + 2, series: { p: { lat: [[t, 0.1], [t + 2, 0.4]] } } });
    await mod.refreshStandbySeries(['p']);
    expect(mod.seriesBuffers.get('p')!.lat).toEqual([[t, 0.1], [t + 2, 0.4]]);
  });
});

describe('refreshStandbyGeo / geoText', () => {
  it('按 bind 缓存归属地；未解析出的 IP 不写缓存，展示回落 null', async () => {
    vi.resetModules();
    const mod = await import('@/composables/useStandbyGeo');
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        geo: {
          '1.2.3.4:8080': { country: '美国', country_code: 'US', province: '加州', city: '洛杉矶' },
          '5.6.7.8:8080': null,
        },
      }),
    })));
    await mod.refreshStandbyGeo(true);
    expect(mod.geoText(mod.standbyGeo.get('1.2.3.4:8080'))).toBe('美国 · 加州 · 洛杉矶');
    expect(mod.geoText(mod.standbyGeo.get('5.6.7.8:8080'))).toBeNull();
  });

  it('geoip 关闭时不发请求', async () => {
    vi.resetModules();
    const mod = await import('@/composables/useStandbyGeo');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await mod.refreshStandbyGeo(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('geoText', () => {
  it('省市缺失时逐级退化，仅国家级也能展示，无归属返回 null', () => {
    expect(geoText({ country: '日本', country_code: 'JP', province: null, city: null })).toBe('日本');
    expect(geoText({ country: '美国', country_code: 'US', province: '加州', city: null })).toBe('美国 · 加州');
    expect(geoText(undefined)).toBeNull();
  });
});
