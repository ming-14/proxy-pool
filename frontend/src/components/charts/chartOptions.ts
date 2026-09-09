/**
 * 折线图 option 工厂：统一产出与旧 Chart.js 渲染一致的视觉参数
 * （颜色/网格/图例圆环/tooltip 卡片/对数轴/平滑/面积填充）。
 * 颜色从 CSS 变量读取（theme.css 单一来源），主题切换时由调用方重建 option。
 */
import type { EChartsOption } from './echarts';

export interface LineTheme {
  primary: string;
  success: string;
  warning: string;
  text: string;
  grid: string;
  cardBg: string;
  textPrimary: string;
}

/** 读取当前主题色（documentElement 上的 --color-* 令牌） */
export function readLineTheme(): LineTheme {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    primary: v('--color-primary-500', '#3b82f6'),
    success: v('--color-success', '#10b981'),
    warning: v('--color-warning', '#f59e0b'),
    text: v('--color-t-secondary', '#64748b'),
    grid: v('--color-line', '#e2e8f0'),
    cardBg: v('--color-card', '#ffffff'),
    textPrimary: v('--color-t-primary', '#0f172a'),
  };
}

export interface SeriesCfg {
  name: string;
  data: (number | null)[];
  color: string;
  /** 面积填充（十六进制色 + alpha 后缀，与旧 '#20'/'#15' 用法一致） */
  fillAlpha?: string;
  yAxisIndex?: number;
}

export interface AxisCfg {
  type: 'log' | 'value';
  name?: string;
  position?: 'left' | 'right';
  min?: number;
  max?: number;
}

export interface LineChartCfg {
  labels: string[];
  series: SeriesCfg[];
  axes: AxisCfg[];
  legend?: boolean;
}

export function lineChartOption(cfg: LineChartCfg, theme: LineTheme): EChartsOption {
  const axes = cfg.axes.length ? cfg.axes : [{ type: 'value' as const }];
  // 空数据兜底：ECharts 类目轴零类目时整轴不渲染（旧 Chart.js 仍画坐标框架）。
  // 占位空类目 + 显式轴界让空图保留网格骨架，与旧版观感一致。
  const empty = cfg.labels.length === 0;
  return {
    animation: false,
    // containLabel 为 echarts 6 legacy 选项（模块化构建未注册，仅产生告警），布局由默认 outerBounds 处理
    grid: { left: 6, right: 6, top: cfg.legend === false ? 8 : 34, bottom: 2 },
    legend: {
      show: cfg.legend !== false,
      top: 0,
      icon: 'circle',
      itemWidth: 9,
      itemHeight: 9,
      itemGap: 12,
      textStyle: { color: theme.text, fontSize: 12 },
      // Chart.js usePointStyle 圆环：描边=线色，填充=面积色（无填充时透明）
      data: cfg.series.map((s) => ({
        name: s.name,
        itemStyle: {
          color: s.fillAlpha ? s.color + s.fillAlpha : 'transparent',
          borderColor: s.color,
          borderWidth: 1.5,
        },
      })),
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: theme.cardBg,
      borderColor: theme.grid,
      borderWidth: 1,
      padding: 10,
      textStyle: { color: theme.text, fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: theme.grid } },
      valueFormatter: (val) =>
        typeof val === 'number' ? String(Number(val.toFixed(3))) : String(val ?? '-'),
    },
    xAxis: {
      type: 'category',
      data: empty ? [''] : cfg.labels,
      boundaryGap: false,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontSize: 11 },
      splitLine: { show: true, lineStyle: { color: theme.grid } },
    },
    yAxis: axes.map((ax) => buildAxis(ax, theme, empty)),
    series: cfg.series.map((s) => ({
      name: s.name,
      type: 'line' as const,
      data: s.data,
      smooth: 0.35,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 8,
      lineStyle: { width: 2, color: s.color },
      itemStyle: { color: s.color },
      areaStyle: s.fillAlpha ? { color: s.color + s.fillAlpha } : undefined,
      yAxisIndex: s.yAxisIndex ?? 0,
    })),
  };
}

function buildAxis(ax: AxisCfg, theme: LineTheme, empty = false) {
  const isRight = ax.position === 'right';
  // 空数据时 ECharts 无法从序列推界（value 轴只剩 min 也出不来刻度）→ 显式兜底区间
  const fallback = empty ? (ax.type === 'log' ? { min: 0.01, max: 10 } : { min: 0, max: 1 }) : null;
  return {
    type: ax.type,
    position: isRight ? ('right' as const) : ('left' as const),
    name: ax.name,
    nameLocation: ax.name ? ('middle' as const) : ('end' as const),
    nameGap: ax.name ? (isRight ? 38 : 36) : 0,
    nameRotate: ax.name ? (isRight ? -90 : 90) : 0,
    nameTextStyle: { color: theme.text, fontSize: 11 },
    // 线性左轴默认 0 起点（Chart.js beginAtZero），log 轴不强制
    min: ax.min ?? fallback?.min ?? (ax.type === 'value' && !isRight ? 0 : undefined),
    max: ax.max ?? fallback?.max,
    // Chart.js 对数轴刻度带千分位（1,000 / 50,000）
    axisLabel: {
      color: theme.text,
      fontSize: 11,
      formatter: (val: number) =>
        ax.type === 'log' ? val.toLocaleString('en-US') : String(val),
    },
    axisLine: { show: false },
    axisTick: { show: false },
    // 右轴不画横向网格（对齐 Chart.js drawOnChartArea:false）
    splitLine: { show: !isRight, lineStyle: { color: theme.grid } },
  };
}
