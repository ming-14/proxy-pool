<script setup lang="ts">
/**
 * 右键上下文菜单：Teleport 到 body，支持二级子菜单（hover 展开），
 * 点击外部/滚动/缩放自动关闭（关闭逻辑收敛于此，替代旧版两页面重复实现）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { MenuItem } from '@/types/ui';

const props = defineProps<{ items: MenuItem[]; x: number; y: number }>();
const emit = defineEmits<{
  (e: 'select', item: MenuItem): void;
  (e: 'close'): void;
}>();

const el = ref<HTMLElement | null>(null);
/** 实际渲染坐标（夹取后）。不能直接绑 props.x/y：菜单已打开时再次右键只走重渲染，
 *  重渲染会把手工写入的夹取结果覆盖回原始点击点，菜单就溢出视口了。 */
const pos = ref({ x: props.x, y: props.y });
/** 首次定位完成前隐藏，避免未夹取的坐标闪一帧 */
const placed = ref(false);

/** 夹取到视口内：宽高与位置无关，隐藏态下量完尺寸再定坐标即可 */
function clampPosition() {
  const node = el.value;
  if (!node) return;
  const rect = node.getBoundingClientRect();
  pos.value = {
    x: Math.min(Math.max(props.x, 8), window.innerWidth - rect.width - 8),
    y: Math.min(Math.max(props.y, 8), window.innerHeight - rect.height - 8),
  };
  placed.value = true;
}

watch(() => [props.x, props.y] as const, () => clampPosition());

function onDocClick(e: MouseEvent) {
  if (el.value && !el.value.contains(e.target as Node)) emit('close');
}

function onItem(item: MenuItem) {
  if (item.action) emit('select', item);
}

onMounted(async () => {
  await nextTick();
  clampPosition();
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('scroll', close, true);
  window.addEventListener('resize', close);
});

function close() {
  emit('close');
}

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick, true);
  document.removeEventListener('scroll', close, true);
  window.removeEventListener('resize', close);
});
</script>

<template>
  <Teleport to="body">
    <div
      ref="el"
      class="context-menu"
      :style="{ left: `${pos.x}px`, top: `${pos.y}px`, visibility: placed ? 'visible' : 'hidden' }"
    >
      <template v-for="(item, i) in items" :key="i">
        <div
          class="context-menu-item"
          :class="{ 'context-menu-danger': item.danger, 'context-menu-has-submenu': item.children }"
          @click.stop="onItem(item)"
        >
          <span>{{ item.label }}</span>
          <svg v-if="item.children" class="context-menu-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6" /></svg>
          <span v-else-if="item.meta" class="text-[var(--color-t-muted)] text-xs">{{ item.meta }}</span>
          <div v-if="item.children" class="context-submenu">
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
