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
} from '@/utils/format';

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
