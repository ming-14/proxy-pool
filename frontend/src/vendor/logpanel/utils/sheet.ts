/**
 * 抽屉形态的纯计算：形态判定、高度/键盘内缩换算、拖拽定档。
 *
 * 全部无 DOM 依赖，便于单测覆盖（软键盘只在真机上出现，无头浏览器复现不了）。
 */
import {
  MIN_SHEET_H,
  SHEET_DETENT_RATIO,
  SHEET_FLING_MIN_DY,
  SHEET_FLING_V,
  SHEET_HALF_RATIO,
} from '../defaults';
import type { LogPanelDetent, LogPanelForm } from '../types';

/** 形态判定：显式指定优先，auto 按窄屏媒体查询 */
export function resolveForm(form: LogPanelForm, narrow: boolean): 'floating' | 'sheet' {
  return form === 'auto' ? (narrow ? 'sheet' : 'floating') : form;
}

/** 档位高度：半屏按可视视口比例，全屏铺满可视视口；下限 MIN_SHEET_H、上限可视高度 */
export function sheetHeight(detent: LogPanelDetent, visualHeight: number): number {
  const ratio = detent === 'full' ? 1 : SHEET_HALF_RATIO;
  return Math.round(Math.min(Math.max(visualHeight * ratio, MIN_SHEET_H), visualHeight));
}

/**
 * 软键盘/浏览器 UI 占去的底部内缩：可视视口顶边到布局视口底边的距离。
 * 抽屉的 bottom 取此值后，全屏档 + 键盘弹出时抽屉恰好铺满可视视口（顶边贴可视视口顶边）。
 */
export function keyboardInset(layoutHeight: number, visualHeight: number, visualTop: number): number {
  return Math.max(0, Math.round(layoutHeight - visualHeight - visualTop));
}

/** 拖拽中的实时位移夹取：向上不越过全屏档，向下不越过半屏档 */
export function clampOffset(offset: number, detent: LogPanelDetent, visualHeight: number): number {
  const half = sheetHeight('half', visualHeight);
  const full = sheetHeight('full', visualHeight);
  const min = detent === 'full' ? 0 : half - full; // 半屏档可向上拖到全屏（负值）
  const max = detent === 'full' ? full - half : 0; // 全屏档可向下拖到半屏（正值）
  return Math.min(Math.max(offset, min), max);
}

/**
 * 抬手定档：快速滑动（位移够大且速度过阈值）按方向翻档，否则按位移占档位行程的比例取更近的档。
 * durationMs 为 0（无时间信息）时只按位移判定。
 */
export function detentAfterDrag(
  start: LogPanelDetent,
  dy: number,
  durationMs: number,
  visualHeight: number,
): LogPanelDetent {
  if (Math.abs(dy) >= SHEET_FLING_MIN_DY && durationMs > 0 && Math.abs(dy) / durationMs >= SHEET_FLING_V) {
    return dy < 0 ? 'full' : 'half';
  }
  const travel = sheetHeight('full', visualHeight) - sheetHeight('half', visualHeight);
  if (travel <= 0) return start;
  const ratio = dy / travel; // 向上拖为负 → 变高
  if (ratio <= -SHEET_DETENT_RATIO) return 'full';
  if (ratio >= SHEET_DETENT_RATIO) return 'half';
  return start;
}
