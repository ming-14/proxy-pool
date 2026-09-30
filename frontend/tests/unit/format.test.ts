/** 格式化纯函数：输出必须与旧前端 utils 逐值一致（视觉不变约束） */
import { describe, expect, it } from 'vitest';
import {
  bindOf,
  displayCountryName,
  escapeHtml,
  fmtAge,
  fmtBytes,
  fmtBytesSpaced,
  fmtDuration,
  fmtLatency,
  fmtTime,
  speedAllFailed,
  speedSummary,
  speedSummaryTitle,
} from '@/utils/format';
import type { SpeedLeg, SpeedTestResult } from '@/types/api';

describe('fmtBytes', () => {
  it('各量级格式与旧 utils 一致', () => {
    expect(fmtBytes(null)).toBe('-');
    expect(fmtBytes(512)).toBe('512B');
    expect(fmtBytes(1024)).toBe('1.0KB');
    expect(fmtBytes(1536)).toBe('1.5KB');
    expect(fmtBytes(1048576)).toBe('1.0MB');
    expect(fmtBytes(1073741824)).toBe('1.00GB');
    expect(fmtBytes(3145728)).toBe('3.0MB');
  });
  it('B 档浮点输入取整，不显示长串小数', () => {
    expect(fmtBytes(64.43327449829533)).toBe('64B');
    expect(fmtBytes(64.5)).toBe('65B');
  });
});

describe('fmtBytesSpaced', () => {
  it('流量 KPI 格式（带空格、B 取整）与旧 dashboard 一致', () => {
    expect(fmtBytesSpaced(null)).toBe('-');
    expect(fmtBytesSpaced(532.7)).toBe('533 B');
    expect(fmtBytesSpaced(2936012)).toBe('2.8 MB');
  });
});

describe('fmtDuration', () => {
  it('秒/分秒/整分', () => {
    expect(fmtDuration(null)).toBe('-');
    expect(fmtDuration(59)).toBe('59s');
    expect(fmtDuration(125)).toBe('2m 5s');
    expect(fmtDuration(120)).toBe('2m');
  });
});

describe('fmtAge', () => {
  it('粗粒度时长：分钟/小时/天（秒级粒度不进展示）', () => {
    expect(fmtAge(null)).toBe('-');
    expect(fmtAge(-1)).toBe('-');
    expect(fmtAge(0)).toBe('不到 1 分钟');
    expect(fmtAge(59)).toBe('不到 1 分钟');
    expect(fmtAge(600)).toBe('10 分钟');
    expect(fmtAge(3600)).toBe('1 小时 0 分');
    expect(fmtAge(9000)).toBe('2 小时 30 分');
    expect(fmtAge(90000)).toBe('1 天 1 小时');
  });
});

describe('fmtLatency / bindOf', () => {
  it('延迟三位小数', () => {
    expect(fmtLatency(null)).toBe('-');
    expect(fmtLatency(0.29)).toBe('0.290s');
  });
  it('bindOf 去 scheme', () => {
    expect(bindOf('http://1.2.3.4:8080')).toBe('1.2.3.4:8080');
    expect(bindOf('1.2.3.4:8080')).toBe('1.2.3.4:8080');
  });
});

describe('fmtTime', () => {
  it('秒级时间戳 → HH:MM:SS（24h 制，无 AM/PM）', () => {
    expect(fmtTime(1700000000)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(fmtTime(1700000000)).not.toMatch(/[AP]M/i);
  });

  it('接受小数秒（Date.now()/1000 这类本地完成时刻）', () => {
    expect(fmtTime(Date.now() / 1000)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});

describe('escapeHtml', () => {
  it('转义 & < > "', () => {
    expect(escapeHtml('<a href="x">&</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  });
});

describe('displayCountryName', () => {
  it('中国/China 映射，其余原样', () => {
    expect(displayCountryName('中国')).toBe('中国大陆');
    expect(displayCountryName('China')).toBe("China's Mainland");
    expect(displayCountryName('美国')).toBe('美国');
  });
});

function leg(ok: boolean, extra: Partial<SpeedLeg> = {}): SpeedLeg {
  return { ok, best_mbps: 0, ...extra };
}

function result(download: SpeedLeg | null, upload: SpeedLeg | null): SpeedTestResult {
  return { proxy: 'http://1.2.3.4:8080', download, upload };
}

describe('测速结果展示', () => {
  it('失败腿显示"失败"而不是 0.0，成功腿显示速率', () => {
    const r = result(
      leg(false, { err: 'IncompleteRead: 71610 bytes read' }),
      leg(true, { best_mbps: 20.44, node: 'cloudflare', ms: 1234.5 }),
    );
    expect(speedSummary(r)).toBe('↓失败 ↑20.4 Mbps');
    expect(speedSummaryTitle(r)).toBe(
      '下载：失败（IncompleteRead: 71610 bytes read）｜上传：20.44 Mbps（节点 cloudflare，用时 1235ms）',
    );
  });

  it('两腿都失败时不显示速率单位，并按失败着色', () => {
    const r = result(leg(false, { err: 'HTTP 100' }), leg(false, { err: '读取超时' }));
    expect(speedSummary(r)).toBe('↓失败 ↑失败');
    expect(speedAllFailed(r)).toBe(true);
  });

  it('无结果腿显示 -，且不算全失败', () => {
    const r = result(null, leg(true, { best_mbps: 1.25 }));
    expect(speedSummary(r)).toBe('↓- ↑1.3 Mbps');
    expect(speedSummaryTitle(result(null, null))).toBe('下载：无结果｜上传：无结果');
    expect(speedAllFailed(r)).toBe(false);
  });

  it('失败腿缺 err 时给出兜底原因', () => {
    expect(speedSummaryTitle(result(leg(false), null))).toBe('下载：失败（原因未知）｜上传：无结果');
  });
});
