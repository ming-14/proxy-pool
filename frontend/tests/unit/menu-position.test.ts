/** 右键菜单定位：等价 Win32 菜单管理器的屏内摆放规则 */
import { describe, expect, it } from 'vitest';
import { MENU_GAP, clampMenu, placeSubmenu } from '@/utils/menuPosition';

const VIEWPORT = { width: 1000, height: 800 };

describe('clampMenu', () => {
  it('光标在左上：原样落位', () => {
    expect(clampMenu({ x: 100, y: 90 }, { width: 160, height: 120 }, VIEWPORT)).toEqual({ x: 100, y: 90 });
  });

  it('越过右下：整体左移上移，贴边保留留白', () => {
    const p = clampMenu({ x: 980, y: 790 }, { width: 160, height: 120 }, VIEWPORT);
    expect(p).toEqual({ x: VIEWPORT.width - 160 - MENU_GAP, y: VIEWPORT.height - 120 - MENU_GAP });
  });

  it('越过左上：贴左上留白', () => {
    expect(clampMenu({ x: -50, y: -30 }, { width: 160, height: 120 }, VIEWPORT)).toEqual({
      x: MENU_GAP,
      y: MENU_GAP,
    });
  });

  it('菜单比视口还大：贴左上，不出现负坐标把菜单推出屏幕', () => {
    const p = clampMenu({ x: 500, y: 400 }, { width: 1200, height: 900 }, VIEWPORT);
    expect(p).toEqual({ x: MENU_GAP, y: MENU_GAP });
  });
});

describe('placeSubmenu', () => {
  const size = { width: 160, height: 120 };

  it('右侧装得下：贴右侧，垂直与父项顶对齐', () => {
    const p = placeSubmenu({ top: 100, bottom: 130, left: 200, right: 360 }, size, VIEWPORT);
    expect(p).toEqual({ side: 'right', top: 100 });
  });

  it('右侧装不下、左侧装得下：翻到左侧', () => {
    const p = placeSubmenu({ top: 100, bottom: 130, left: 800, right: 960 }, size, VIEWPORT);
    expect(p.side).toBe('left');
  });

  it('两侧都装不下：取余量大的一侧', () => {
    const item = { top: 100, bottom: 130, left: 400, right: 560 };
    const wide = { width: 500, height: 120 }; // 视口 1000：右余 432、左余 392 → 右
    expect(placeSubmenu(item, wide, VIEWPORT).side).toBe('right');
    // 视口收窄到 900：右余 332、左余 392 → 左
    expect(placeSubmenu(item, wide, { width: 900, height: 800 }).side).toBe('left');
  });

  it('下方放不下：上移贴视口底', () => {
    const p = placeSubmenu({ top: 760, bottom: 790, left: 200, right: 360 }, size, VIEWPORT);
    expect(p).toEqual({ side: 'right', top: VIEWPORT.height - size.height - MENU_GAP });
  });

  it('上方放不下（负坐标锚点）：下移到留白处', () => {
    expect(placeSubmenu({ top: -20, bottom: 10, left: 200, right: 360 }, size, VIEWPORT).top).toBe(MENU_GAP);
  });

  it('子菜单比视口还高：贴顶，余下由内部滚动兜住', () => {
    const p = placeSubmenu({ top: 400, bottom: 430, left: 200, right: 360 }, { width: 160, height: 900 }, VIEWPORT);
    expect(p.top).toBe(MENU_GAP);
  });
});