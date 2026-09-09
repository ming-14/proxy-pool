/**
 * ECharts 按需注册（tree-shake）：折线/散点 + 直角坐标/geo/visualMap + canvas 渲染器。
 * 全项目唯一 echarts 入口，页面不得直接 import 'echarts'。
 */
import * as echarts from 'echarts/core';
import { LineChart, ScatterChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  GeoComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  GeoComponent,
  CanvasRenderer,
]);

export { echarts };
export type { EChartsOption, EChartsType } from 'echarts';
