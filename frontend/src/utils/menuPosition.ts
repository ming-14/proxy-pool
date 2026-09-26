/**
 * 右键菜单定位：等价 Win32 菜单管理器（TrackPopupMenu）的屏内摆放规则。
 *
 * 三条规则：
 * 1. 主菜单以光标为锚点，右/下越界就整体左移/上移，贴边为止（不裁剪）；
 * 2. 子菜单右侧放不下就翻到父项左侧（且取余量更大的一侧），垂直放不下则上移；
 * 3. 视口取 clientWidth/Height（排除滚动条），对应 Win32 的"工作区"而非整屏。
 *
 * 纯函数，可单测。
 */

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

/** 视口坐标下的矩形（父项的 getBoundingClientRect 语义） */
export interface BoxRect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface SubmenuPlacement {
  side: 'left' | 'right';
  /** 视口坐标下的上边；相对父项的偏移由调用方换算 */
  top: number;
}

/** 菜单与视口边缘的最小留白 */
export const MENU_GAP = 8;

/** 夹取；区间退化（菜单比视口还大）时贴低界，避免算出负坐标把菜单推出屏幕 */
function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), Math.max(lo, hi));
}

/** 视口尺寸：clientWidth/Height 已排除滚动条，等价 Win32 的"工作区" */
export function viewportSize(): Size {
  const el = document.documentElement;
  return { width: el.clientWidth, height: el.clientHeight };
}

/** 主菜单：以光标为锚点，越界整体平移进视口 */
export function clampMenu(anchor: Point, size: Size, viewport: Size, gap = MENU_GAP): Point {
  return {
    x: clamp(anchor.x, gap, viewport.width - size.width - gap),
    y: clamp(anchor.y, gap, viewport.height - size.height - gap),
  };
}

/** 子菜单翻边：优先右侧，右侧装不下翻左侧，两侧都装不下取余量大的一侧 */
function pickSide(roomRight: number, roomLeft: number, width: number): SubmenuPlacement['side'] {
  if (roomRight >= width) return 'right';
  if (roomLeft >= width) return 'left';
  return roomRight >= roomLeft ? 'right' : 'left';
}

/**
 * 子菜单：水平优先贴父项右侧，装不下翻到左侧；
 * 垂直默认与父项顶部对齐，下方放不下则上移（贴视口底），上方放不下则下移。
 */
export function placeSubmenu(
  item: BoxRect,
  size: Size,
  viewport: Size,
  gap = MENU_GAP,
): SubmenuPlacement {
  return {
    side: pickSide(viewport.width - item.right - gap, item.left - gap, size.width),
    top: clamp(item.top, gap, viewport.height - size.height - gap),
  };
}