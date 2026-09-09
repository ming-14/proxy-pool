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

function clampPosition() {
  const node = el.value;
  if (!node) return;
  node.style.visibility = 'hidden';
  const rect = node.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 8;
  const maxY = window.innerHeight - rect.height - 8;
  node.style.left = `${Math.min(Math.max(props.x, 8), maxX)}px`;
  node.style.top = `${Math.min(Math.max(props.y, 8), maxY)}px`;
  node.style.visibility = '';
}

watch(() => [props.x, props.y], () => clampPosition());

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
    <div ref="el" class="context-menu" :style="{ left: `${x}px`, top: `${y}px`, visibility: 'hidden' }">
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
