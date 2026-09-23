/**
 * 组件内置图标：24 网格、stroke-width 2、圆端点的统一规格（与宿主图标规范一致）。
 * 内容为常量 SVG 片段，由 IconSvg 以内联方式渲染。
 */

export const ICONS = {
  title: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  pause: '<line x1="9" y1="4" x2="9" y2="20"/><line x1="15" y1="4" x2="15" y2="20"/>',
  play: '<polygon points="7 4 19 12 7 20 7 4"/>',
  'scroll-end': '<path d="M12 3v16M6 13l6 6 6-6"/>',
  'scroll-free': '<path d="M12 21V5M6 11l6-6 6 6"/>',
  clear: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="1"/><path d="M5 15V5a1 1 0 011-1h10"/>',
  export: '<path d="M12 3v12M7 10l5 5 5-5M4 19h16"/>',
  'chevron-up': '<polyline points="6 15 12 9 18 15"/>',
  'chevron-down': '<polyline points="6 9 12 15 18 9"/>',
  filter: '<path d="M4 5h16l-6 7v6l-4 2v-8z"/>',
} as const;

export type PanelIconName = keyof typeof ICONS;