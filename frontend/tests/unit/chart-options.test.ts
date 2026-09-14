/** 折线图 option 工厂：对数轴非正值不绘制、逐轴兜底区间、轴名与刻度文本 */
import { describe, expect, it } from 'vitest';
import { lineChartOption, type LineTheme } from '@/components/charts/chartOptions';

const theme: LineTheme = {
  primary: '#3b82f6',
  success: '#10b981',
  warning: '#f59e0b',
  text: '#64748b',
  grid: '#e2e8f0',
  cardBg: '#ffffff',
  textPrimary: '#0f172a',
};

interface AxisView {
  name?: string;
  min?: number;
  max?: number;
  axisLabel: { formatter: (val: number) => string };
}
interface OptionView {
  yAxis: AxisView[];
  series: { data: (number | null)[]; yAxisIndex: number }[];
}

const optionOf = (cfg: Parameters<typeof lineChartOption>[0]) =>
  lineChartOption(cfg, theme) as unknown as OptionView;

describe('lineChartOption 对数轴', () => {
  it('0 与负值置 null：既不绘制也不参与值域', () => {
    const opt = optionOf({
      labels: ['a', 'b', 'c'],
      series: [{ name: '上行', data: [1024, 0, -5], color: '#000' }],
      axes: [{ type: 'log' }],
    });
    expect(opt.series[0].data).toEqual([1024, null, null]);
  });

  it('线性轴保留 0（健康率/队列长度靠 0 读数）', () => {
    const opt = optionOf({
      labels: ['a', 'b'],
      series: [{ name: '队列长度', data: [0, 3], color: '#000' }],
      axes: [{ type: 'value' }],
    });
    expect(opt.series[0].data).toEqual([0, 3]);
  });

  it('该轴全部不可绘制时套兜底区间（刻度与轴名照常显示）', () => {
    const opt = optionOf({
      labels: ['a', 'b'],
      series: [{ name: '上行', data: [0, 0], color: '#000' }],
      axes: [{ type: 'log', name: '字节/s' }],
    });
    expect(opt.yAxis[0]).toMatchObject({ name: '字节/s', min: 0.01, max: 10 });
  });

  it('有数据的轴不套兜底区间（值域由数据决定）', () => {
    const opt = optionOf({
      labels: ['a', 'b'],
      series: [{ name: '上行', data: [1024, 2048], color: '#000' }],
      axes: [{ type: 'log', name: '字节/s' }],
    });
    expect(opt.yAxis[0].min).toBeUndefined();
    expect(opt.yAxis[0].max).toBeUndefined();
  });

  it('逐轴判断：空左轴不把右侧有数据的延迟轴带成兜底', () => {
    const opt = optionOf({
      labels: ['a', 'b'],
      series: [
        { name: '上行', data: [0, 0], color: '#000' },
        { name: '平均延迟', data: [0.2, 0.3], color: '#000', yAxisIndex: 1 },
      ],
      axes: [
        { type: 'log', name: '字节/s' },
        { type: 'log', position: 'right', name: '延迟 s' },
      ],
    });
    expect(opt.yAxis[0].min).toBe(0.01);
    expect(opt.yAxis[1].min).toBeUndefined();
    expect(opt.series[1].yAxisIndex).toBe(1);
  });
});

describe('lineChartOption 刻度文本', () => {
  const tickOf = (type: 'log' | 'value') =>
    optionOf({
      labels: ['a'],
      series: [{ name: 's', data: [1], color: '#000' }],
      axes: [{ type }],
    }).yAxis[0].axisLabel.formatter;

  it('对数轴小数按有效位显示（1e-4 不再被截成 "0"）', () => {
    const f = tickOf('log');
    expect(f(0.0001)).toBe('0.0001');
    expect(f(0.001)).toBe('0.001');
    expect(f(0.05)).toBe('0.05');
  });

  it('对数轴大数保留千分位（50,000）', () => {
    expect(tickOf('log')(50000)).toBe('50,000');
  });

  it('线性轴按原值输出', () => {
    const f = tickOf('value');
    expect(f(100)).toBe('100');
    expect(f(0)).toBe('0');
  });
});
