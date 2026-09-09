/** CSV 构建：引号转义与列拼接与旧导出一致 */
import { describe, expect, it } from 'vitest';
import { buildCsv, csvEscape } from '@/utils/csv';

describe('csvEscape', () => {
  it('包裹双引号并转义内部引号', () => {
    expect(csvEscape('a"b')).toBe('"a""b"');
    expect(csvEscape(null)).toBe('""');
    expect(csvEscape(12)).toBe('"12"');
  });
});

describe('buildCsv', () => {
  it('表头原样、行转义', () => {
    const csv = buildCsv(['proxy', 'latency'], [['http://1.1.1.1:8', 0.5]]);
    const lines = csv.split('\n');
    expect(lines[0]).toBe('proxy,latency');
    expect(lines[1]).toBe('"http://1.1.1.1:8","0.5"');
  });
});
