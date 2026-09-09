/** UI 组件共享类型 */
export interface MenuItem {
  label: string;
  action?: string;
  group?: string;
  danger?: boolean;
  children?: MenuItem[];
  meta?: string; // 右侧灰色附注（如组内代理数）
}
