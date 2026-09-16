<script setup lang="ts">
/**
 * 面板图标：把 icons.ts 的图标内容渲染成统一规格的 SVG。
 *
 * 规格（24 网格 / stroke-width 2 / round 端点与连接 / fill:none）在组件上统一施加，
 * 调用方只需给名字与尺寸/颜色：
 *   <Icon name="play" class="w-4 h-4 text-[var(--color-primary-600)]" />
 *
 * 尺寸走 CSS（Tailwind 的宽度/高度类）与颜色（text-* 类），不另设 size prop——
 * 面板本就用类控尺寸，多一条路径只会让"到底谁生效"变模糊。width/height 属性仅作
 * 无类时的兜底，CSS 优先级高于表现属性，给了类就以类为准。class 由 Vue 属性透传
 * 落到根 svg 元素。
 */
import { computed } from 'vue';
import { ICONS, type IconName } from './icons';

const props = defineProps<{ name: IconName }>();

const body = computed(() => ICONS[props.name]);
</script>

<template>
  <svg
    viewBox="0 0 24 24"
    width="16"
    height="16"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="body"
  />
</template>
