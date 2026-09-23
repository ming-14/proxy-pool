/**
 * 抽屉形态纯计算单测：形态判定、档位高度、键盘内缩、拖拽位移夹取与抬手定档。
 *
 * 软键盘只在真机上出现（无头浏览器复现不了），这几条纯函数是它的正确性来源。
 */
import { describe, expect, it } from 'vitest';
import { clampOffset, detentAfterDrag, keyboardInset, resolveForm, sheetHeight } from '@/vendor/logpanel/utils/sheet';

describe('resolveForm', () => {
  it('auto 按窄屏判定', () => {
    expect(resolveForm('auto', true)).toBe('sheet');
    expect(resolveForm('auto', false)).toBe('floating');
  });

  it('显式指定优先于视口宽度', () => {
    expect(resolveForm('sheet', false)).toBe('sheet');
    expect(resolveForm('floating', true)).toBe('floating');
  });
});

describe('sheetHeight', () => {
  it('半屏按比例、全屏铺满', () => {
    expect(sheetHeight('half', 844)).toBe(464);
    expect(sheetHeight('full', 844)).toBe(844);
  });

  it('不足最小高度时兜底，且不超过可视高度', () => {
    expect(sheetHeight('half', 300)).toBe(240);
    expect(sheetHeight('half', 200)).toBe(200);
  });
});

describe('keyboardInset', () => {
  it('无键盘时为零', () => {
    expect(keyboardInset(844, 844, 0)).toBe(0);
  });

  it('键盘占去的高度即内缩量，可视视口上移时相应减少', () => {
    expect(keyboardInset(844, 400, 0)).toBe(444);
    expect(keyboardInset(844, 400, 100)).toBe(344);
  });

  it('可视视口比布局视口更高（缩放）时不出现负数', () => {
    expect(keyboardInset(844, 900, 0)).toBe(0);
  });
});

describe('clampOffset', () => {
  it('半屏档只允许向上拖到全屏', () => {
    expect(clampOffset(-100, 'half', 844)).toBe(-100);
    expect(clampOffset(-500, 'half', 844)).toBe(-380);
    expect(clampOffset(50, 'half', 844)).toBe(0);
  });

  it('全屏档只允许向下拖到半屏', () => {
    expect(clampOffset(50, 'full', 844)).toBe(50);
    expect(clampOffset(500, 'full', 844)).toBe(380);
    expect(clampOffset(-50, 'full', 844)).toBe(0);
  });
});

describe('detentAfterDrag', () => {
  it('慢拖按位移取更近的档', () => {
    expect(detentAfterDrag('half', -200, 400, 844)).toBe('full');
    expect(detentAfterDrag('full', 260, 400, 844)).toBe('half');
  });

  it('位移未过阈值时保持原档', () => {
    expect(detentAfterDrag('half', -40, 400, 844)).toBe('half');
    expect(detentAfterDrag('full', 40, 400, 844)).toBe('full');
  });

  it('快速滑动按方向翻档', () => {
    expect(detentAfterDrag('half', -60, 50, 844)).toBe('full');
    expect(detentAfterDrag('full', 60, 50, 844)).toBe('half');
  });

  it('快速但位移过小时不认速度', () => {
    expect(detentAfterDrag('half', -20, 10, 844)).toBe('half');
  });

  it('无时间信息时只按位移', () => {
    expect(detentAfterDrag('half', -200, 0, 844)).toBe('full');
  });
});
