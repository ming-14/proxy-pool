<script setup lang="ts">
/**
 * 内置图标：渲染 utils/icons 的常量 SVG 片段（24 网格 / stroke-2 统一规格）。
 */
import { ICONS, type PanelIconName } from '../utils/icons';

defineProps<{ name: PanelIconName }>();
</script>

<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="ICONS[name]"
  />
</template>

<style scoped>
svg {
  display: block;
  width: 14px;
  height: 14px;
}
</style>