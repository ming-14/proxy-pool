<script setup lang="ts">
/**
 * 右键上下文菜单：Teleport 到 body，支持二级子菜单。
 * 摆放规则等价 Win32 菜单管理器：主菜单越界整体平移进视口；子菜单右侧放不下翻到父项左侧、
 * 下方放不下上移，比视口还高则内部滚动（不裁剪）。关闭逻辑收敛于此（替代旧版两页面重复实现）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue';
import Icon from './Icon.vue';
import { MENU_GAP, clampMenu, placeSubmenu, viewportSize } from '@/utils/menuPosition';
import type { MenuItem } from '@/types/ui';

const props = defineProps<{ items: MenuItem[]; x: number; y: number }>();
const emit = defineEmits<{
  (e: 'select', item: MenuItem): void;
  (e: 'close'): void;
}>();

const el = ref<HTMLElement | null>(null);
/** 已展开的子菜单标签：桌面 hover 与触屏点击共用（触屏无 hover，两路径互不干扰） */
const openSubmenu = ref<string | null>(null);
/** 子菜单实测位置：翻边方向 + 相对父项的 top */
const submenuPos = ref<{ side: 'left' | 'right'; top: number } | null>(null);
/** 子菜单限高（视口高度 - 两侧留白）：量尺寸前就要生效——超高时出现的滚动条会占宽度，
 *  不放上去会量窄十几像素，翻边判断就可能差这一点点而压出屏幕 */
const submenuLimit = ref(0);
/** 最近一次按下的指针类型：混合设备（触摸屏笔记本）据此决定点击路径是否接管子菜单开合 */
let lastPointerType = 'mouse';

function onPointerDown(e: PointerEvent) {
  lastPointerType = e.pointerType;
}

/** 实际渲染坐标（夹取后）。不能直接绑 props.x/y：菜单已打开时再次右键只走重渲染，
 *  重渲染会把手工写入的夹取结果覆盖回原始点击点，菜单就溢出视口了。 */
const pos = ref({ x: props.x, y: props.y });
/** 首次定位完成前隐藏，避免未夹取的坐标闪一帧 */
const placed = ref(false);

/** 主菜单定位：锚点或内容变化都要重量——items 变了高度就变，沿用旧结果会溢出下缘 */
function layout() {
  const node = el.value;
  if (!node) return;
  pos.value = clampMenu({ x: props.x, y: props.y }, node.getBoundingClientRect(), viewportSize());
  placed.value = true;
}

function resetSubmenu() {
  openSubmenu.value = null;
  submenuPos.value = null;
}

watch(
  () => [props.x, props.y, props.items] as const,
  () => {
    resetSubmenu(); // 换了一份 items：旧的展开目标已失效
    layout();
  },
);

/** 展开子菜单：先按 .open 渲染（隐藏态即可量尺寸）→ 量完定位 → 显示，全程无错位帧 */
async function openSubmenuAt(item: MenuItem, itemEl: HTMLElement) {
  const viewport = viewportSize();
  submenuLimit.value = viewport.height - 2 * MENU_GAP;
  openSubmenu.value = item.label;
  submenuPos.value = null;
  await nextTick();
  if (openSubmenu.value !== item.label) return; // 期间已切走，丢弃这次测量
  const sub = itemEl.querySelector<HTMLElement>('.context-submenu');
  if (!sub) return;
  const r = itemEl.getBoundingClientRect();
  const p = placeSubmenu(
    { top: r.top, bottom: r.bottom, left: r.left, right: r.right },
    { width: sub.offsetWidth, height: sub.offsetHeight },
    viewport,
  );
  submenuPos.value = { side: p.side, top: p.top - r.top }; // 子菜单绝对定位锚在父项上，故换算成相对父项的偏移
}

function onItemClick(item: MenuItem, e: MouseEvent) {
  if (item.children) {
    // 桌面 hover 已负责展开，点击不再二次开合（与资源管理器一致）
    if (lastPointerType !== 'touch') return;
    const itemEl = e.currentTarget as HTMLElement;
    if (openSubmenu.value === item.label) resetSubmenu();
    else void openSubmenuAt(item, itemEl);
    return;
  }
  if (item.action) emit('select', item);
}

function onItemEnter(item: MenuItem, e: PointerEvent) {
  if (e.pointerType === 'touch') return; // 触屏交给点击路径：tap 会合成 pointerenter，不能重复开合
  if (!item.children) {
    resetSubmenu(); // 悬到叶子项：收起已展开的子菜单
    return;
  }
  if (openSubmenu.value !== item.label) void openSubmenuAt(item, e.currentTarget as HTMLElement);
}

/** 只关自己展开的那一个：悬停换项时 pointerleave 与 pointerenter 的先后顺序不参与判定 */
function onItemLeave(item: MenuItem, e: PointerEvent) {
  if (e.pointerType === 'touch') return;
  if (openSubmenu.value === item.label) resetSubmenu();
}

/** 子菜单内联定位：量完前隐藏（此时 display 已由 .open 打开，但还没定位） */
function submenuStyle(label: string): CSSProperties | undefined {
  const p = submenuPos.value;
  if (openSubmenu.value !== label) return undefined;
  return {
    top: p ? `${p.top}px` : undefined,
    maxHeight: `${submenuLimit.value}px`,
    visibility: p ? 'visible' : 'hidden',
  };
}

function onDocClick(e: MouseEvent) {
  if (el.value && !el.value.contains(e.target as Node)) emit('close');
}

/** 页面滚动关闭菜单；菜单内部滚动（子菜单比视口高）不算——否则拖动子菜单滚动条会把自己关掉 */
function onDocScroll(e: Event) {
  if (el.value && el.value.contains(e.target as Node)) return;
  emit('close');
}

onMounted(() => {
  layout();
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('scroll', onDocScroll, true);
  window.addEventListener('resize', close);
});

function close() {
  emit('close');
}

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick, true);
  document.removeEventListener('scroll', onDocScroll, true);
  window.removeEventListener('resize', close);
});
</script>

<template>
  <Teleport to="body">
    <div
      ref="el"
      class="context-menu"
      :style="{ left: `${pos.x}px`, top: `${pos.y}px`, visibility: placed ? 'visible' : 'hidden' }"
      @pointerdown="onPointerDown"
    >
      <template v-for="(item, i) in items" :key="i">
        <div
          class="context-menu-item"
          :class="{
            'context-menu-danger': item.danger,
            'context-menu-flip': item.children && openSubmenu === item.label && submenuPos?.side === 'left',
          }"
          @click.stop="onItemClick(item, $event)"
          @pointerenter="onItemEnter(item, $event)"
          @pointerleave="onItemLeave(item, $event)"
        >
          <span>{{ item.label }}</span>
          <Icon v-if="item.children" name="chevron-right" class="context-menu-arrow" />
          <span v-else-if="item.meta" class="text-[var(--color-t-muted)] text-xs">{{ item.meta }}</span>
          <div
            v-if="item.children"
            class="context-submenu"
            :class="{ open: openSubmenu === item.label }"
            :style="submenuStyle(item.label)"
          >
            <div
              v-for="sub in item.children"
              :key="sub.group"
              class="context-menu-item"
              @click.stop="emit('select', sub)"
            >
              <span>{{ sub.label }}</span>
              <span class="text-[var(--color-t-muted)] text-xs">{{ sub.meta }}</span>
            </div>
          </div>
        </div>
      </template>
    </div>
  </Teleport>
</template>