/** 3D 地球数据纯函数：速率映射单调性与上下界、空数据安全、缺坐标容错、两段弧组装 */
import { describe, expect, it } from 'vitest';
import {
  ARC_DOT_STYLE,
  DASH_TIME_FAST_MS,
  DASH_TIME_SLOW_MS,
  MAX_STROKE,
  MIN_STROKE,
  buildPoints,
  buildProxyArcs,
  buildTargetArcs,
  formatProbeStats,
  rateToDashTime,
  rateToStroke,
  withAlpha,
  type GlobePalette,
} from '@/components/globe/globeData';
import type { GlobeData, GlobeFlow, GlobeNode, GlobeStats } from '@/types/api';

const PALETTE: GlobePalette = {
  self: '#06b6d4',
  proxy: '#2563eb',
  locked: '#f59e0b',
  failed: '#ef4444',
  target: '#10b981',
};

function node(overrides: Partial<GlobeNode> = {}): GlobeNode {
  return {
    bind: '1.2.3.4:8080',
    geo: { country: '美国', country_code: 'US', province: null, city: 'Ashburn', lat: 39, lng: -77.5 },
    latency: 0.3,
    consecutive_fails: 0,
    pinned: false,
    locked: false,
    up_rate: 0,
    down_rate: 0,
    up_inst: 0,
    down_inst: 0,
    ...overrides,
  };
}

function flow(overrides: Partial<GlobeFlow> = {}): GlobeFlow {
  return {
    bind: '1.2.3.4:8080',
    country: '美国',
    country_code: 'US',
    city: 'Ashburn',
    lat: 39,
    lng: -77.5,
    up_rate: 0,
    down_rate: 0,
    up_inst: 0,
    down_inst: 0,
    hosts: ['a.example'],
    ...overrides,
  };
}

function stats(overrides: Partial<GlobeStats> = {}): GlobeStats {
  return {
    active: true,
    probe_ok: 0,
    probe_fail: 0,
    backoff: 0,
    candidates: 0,
    probed: 0,
    pending: 0,
    cycle_ts: 0,
    ...overrides,
  };
}

function data(overrides: Partial<GlobeData> = {}): GlobeData {
  return { self: null, nodes: [], flows: [], stats: stats(), ...overrides };
}

describe('rateToStroke', () => {
  it('0 速率为最小线宽，达到画面最大速率时为最大线宽', () => {
    expect(rateToStroke(0, 1000)).toBe(MIN_STROKE);
    expect(rateToStroke(1000, 1000)).toBe(MAX_STROKE);
    expect(rateToStroke(5000, 1000)).toBe(MAX_STROKE); // 超过参考值封顶
  });

  it('随速率单调递增且有上下界', () => {
    const rates = [0, 1, 10, 1000, 500_000, 1_000_000];
    const strokes = rates.map((r) => rateToStroke(r, 1_000_000));
    for (let i = 1; i < strokes.length; i += 1) {
      expect(strokes[i]).toBeGreaterThan(strokes[i - 1]);
    }
    for (const s of strokes) {
      expect(s).toBeGreaterThanOrEqual(MIN_STROKE);
      expect(s).toBeLessThanOrEqual(MAX_STROKE);
    }
  });

  it('非法值安全：负值/NaN 记最小，maxRate 无效不产 NaN', () => {
    expect(rateToStroke(-5, 100)).toBe(MIN_STROKE);
    expect(rateToStroke(NaN, 100)).toBe(MIN_STROKE);
    expect(rateToStroke(100, 0)).toBe(MIN_STROKE);
    expect(Number.isNaN(rateToStroke(100, NaN))).toBe(false);
  });
});

describe('rateToDashTime', () => {
  it('0 速率最慢，达到参考速率（1MB/s）最快', () => {
    expect(rateToDashTime(0)).toBe(DASH_TIME_SLOW_MS);
    expect(rateToDashTime(1_000_000)).toBe(DASH_TIME_FAST_MS);
    expect(rateToDashTime(50_000_000)).toBe(DASH_TIME_FAST_MS); // 超过参考值封顶
  });

  it('随速率单调递减且有上下界', () => {
    const rates = [0, 1, 1000, 100_000, 1_000_000];
    const times = rates.map(rateToDashTime);
    for (let i = 1; i < times.length; i += 1) {
      expect(times[i]).toBeLessThan(times[i - 1]);
    }
    for (const t of times) {
      expect(t).toBeGreaterThanOrEqual(DASH_TIME_FAST_MS);
      expect(t).toBeLessThanOrEqual(DASH_TIME_SLOW_MS);
    }
  });

  it('非法值安全（NaN 记最慢）', () => {
    expect(rateToDashTime(NaN)).toBe(DASH_TIME_SLOW_MS);
    expect(rateToDashTime(-1)).toBe(DASH_TIME_SLOW_MS);
  });
});

describe('withAlpha', () => {
  it('6 位与 3 位十六进制叠透明度', () => {
    expect(withAlpha('#2563eb', 0.5)).toBe('rgba(37, 99, 235, 0.5)');
    expect(withAlpha('#fff', 1)).toBe('rgba(255, 255, 255, 1)');
  });

  it('不可解析的颜色原样返回（不产非法值）', () => {
    expect(withAlpha('rgb(1, 2, 3)', 0.5)).toBe('rgb(1, 2, 3)');
    expect(withAlpha('', 0.5)).toBe('');
  });
});

describe('formatProbeStats', () => {
  it('还没有探测结果时给出无结果文案', () => {
    expect(formatProbeStats(stats())).toBe('DoH 探测：尚无结果');
  });

  it('成功/失败与取整后的成功率', () => {
    expect(formatProbeStats(stats({ probe_ok: 3, probe_fail: 1 })))
      .toBe('DoH 探测：成功 3 · 失败 1（成功率 75%）');
    expect(formatProbeStats(stats({ probe_ok: 1, probe_fail: 2 })))
      .toBe('DoH 探测：成功 1 · 失败 2（成功率 33%）');
  });

  it('退避中与待探测只在非零时附上', () => {
    expect(formatProbeStats(stats({ probe_ok: 2 }))).toBe('DoH 探测：成功 2 · 失败 0（成功率 100%）');
    expect(formatProbeStats(stats({ probe_ok: 2, backoff: 4, pending: 3 })))
      .toBe('DoH 探测：成功 2 · 失败 0（成功率 100%） · 退避中 4 · 待探测 3');
  });
});

describe('buildPoints', () => {
  it('空数据返回空数组', () => {
    expect(buildPoints(data(), PALETTE)).toEqual([]);
  });

  it('本机点在前，代理节点按状态取色，无坐标节点跳过', () => {
    const points = buildPoints(
      data({
        self: { lat: 31.2, lng: 121.5 },
        nodes: [
          node(),
          node({ bind: '5.6.7.8:1080', locked: true }),
          node({ bind: '9.9.9.9:1', consecutive_fails: 3 }),
          node({ bind: '7.7.7.7:1', geo: null }),
        ],
      }),
      PALETTE,
    );
    expect(points).toHaveLength(4);
    expect(points[0]).toMatchObject({ lat: 31.2, lng: 121.5, color: PALETTE.self });
    expect(points[1].color).toBe(PALETTE.proxy);
    expect(points[2].color).toBe(PALETTE.locked);
    expect(points[3].color).toBe(PALETTE.failed);
    expect(points.some((p) => p.label.includes('7.7.7.7'))).toBe(false);
  });

  it('节点 tooltip 含地址/归属/延迟与速率（HTML 已转义）', () => {
    const points = buildPoints(data({ nodes: [node({ up_rate: 1024 })] }), PALETTE);
    expect(points[0].label).toContain('1.2.3.4:8080');
    expect(points[0].label).toContain('美国 · Ashburn');
    expect(points[0].label).toContain('延迟 0.300s');
    expect(points[0].label).toContain('↑ 1.0 KB/s');
  });
});

describe('buildProxyArcs', () => {
  it('未配置本机坐标时整层隐藏（空数组）', () => {
    expect(buildProxyArcs(data({ nodes: [node()] }), PALETTE)).toEqual([]);
  });

  it('本机 → 节点成弧；0 速率最小线宽，最大速率节点达到上限', () => {
    const arcs = buildProxyArcs(
      data({
        self: { lat: 31.2, lng: 121.5 },
        nodes: [
          node({ bind: 'a:1', up_rate: 0, down_rate: 0 }),
          node({ bind: 'b:1', up_rate: 100, down_rate: 100 }),
          node({ bind: 'c:1', geo: null }),
        ],
      }),
      PALETTE,
    );
    expect(arcs).toHaveLength(2); // 无坐标节点不产弧
    expect(arcs[0]).toMatchObject({ startLat: 31.2, startLng: 121.5, endLat: 39, endLng: -77.5 });
    expect(arcs[0].stroke).toBe(MIN_STROKE);
    expect(arcs[1].stroke).toBe(MAX_STROKE);
  });

  it('流速取瞬时速率、线宽取 EMA（两者可不同）', () => {
    const arcs = buildProxyArcs(
      data({ self: { lat: 0, lng: 0 }, nodes: [node({ up_rate: 10, down_rate: 10, up_inst: 1_000_000 })] }),
      PALETTE,
    );
    expect(arcs[0].dashTime).toBe(DASH_TIME_FAST_MS);
    expect(arcs[0].stroke).toBe(MAX_STROKE); // 单节点即画面最大值
  });
});

describe('buildTargetArcs', () => {
  const withNode = data({
    nodes: [node()],
    flows: [
      flow({ city: 'Ashburn' }),
      flow({ bind: '9.9.9.9:1', city: '巴黎' }),           // bind 不在节点集：不产弧
      flow({ lat: Number.NaN, lng: 2 }),                    // 坐标无效：不产弧
    ],
  });

  it('按 bind 关联节点起点；离队/无坐标不产弧', () => {
    const arcs = buildTargetArcs(withNode, PALETTE);
    expect(arcs).toHaveLength(1);
    expect(arcs[0]).toMatchObject({ startLat: 39, startLng: -77.5, endLat: 39, endLng: -77.5 });
    expect(arcs[0].label).toContain('Ashburn');
    expect(arcs[0].label).toContain('a.example');
  });

  it('空 flows 或节点全无坐标时返回空', () => {
    expect(buildTargetArcs(data({ nodes: [node()] }), PALETTE)).toEqual([]);
    expect(buildTargetArcs(data({ nodes: [node({ geo: null })], flows: [flow()] }), PALETTE)).toEqual([]);
  });

  it('线宽在 flows 内归一：最忙目标达到上限', () => {
    const arcs = buildTargetArcs(
      data({
        nodes: [node()],
        flows: [flow({ up_rate: 1, down_rate: 1 }), flow({ bind: '1.2.3.4:8080', up_rate: 100, down_rate: 100, city: 'B' })],
      }),
      PALETTE,
    );
    expect(arcs[0].stroke).toBe(MIN_STROKE + (MAX_STROKE - MIN_STROKE) * (Math.log1p(2) / Math.log1p(200)));
    expect(arcs[1].stroke).toBe(MAX_STROKE);
  });

  it('线宽只在会画的弧之间归一（离队 flow 不抬高分母）', () => {
    const arcs = buildTargetArcs(
      data({
        nodes: [node()],
        flows: [
          flow({ up_rate: 1, down_rate: 1 }),                                  // log1p(2)
          flow({ up_rate: 3, down_rate: 1, city: 'B' }),                       // log1p(4) → 最大线宽
          flow({ bind: '9.9.9.9:1', up_rate: 1_000_000, down_rate: 0 }),       // bind 不在节点集：不计入归一
        ],
      }),
      PALETTE,
    );
    expect(arcs).toHaveLength(2);
    expect(arcs[0].stroke).toBe(MIN_STROKE + (MAX_STROKE - MIN_STROKE) * (Math.log1p(2) / Math.log1p(4)));
    expect(arcs[1].stroke).toBe(MAX_STROKE);
  });

  it('光点样式为短 dash + 长 gap（单颗流动光点）', () => {
    expect(ARC_DOT_STYLE.dashLength).toBeLessThan(ARC_DOT_STYLE.dashGap);
  });
});
