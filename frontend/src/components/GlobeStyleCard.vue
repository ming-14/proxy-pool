<script setup lang="ts">
/**
 * 地球样式卡片（纯前端偏好，无 API）：球面贴图 4 选 1 + 经纬网/大气光晕开关。
 * 直接读写 useGlobeStyleStore（localStorage 持久化），3D 地球卡片即时跟随。
 */
import { computed } from 'vue';
import {
  GLOBE_TEXTURES,
  globeTextureThumbUrl,
  useGlobeStyleStore,
} from '@/stores/globeStyle';

const globeStyle = useGlobeStyleStore();

const currentHint = computed(
  () => GLOBE_TEXTURES.find((t) => t.id === globeStyle.style.texture)?.hint ?? '',
);
</script>

<template>
  <div data-testid="globe-style-card" class="card">
    <div class="card-header">
      <div class="card-title">地球样式</div>
      <div class="text-xs text-[var(--color-t-muted)]">仅本机浏览器，即时生效</div>
    </div>
    <div class="card-body space-y-4 text-sm">
      <div>
        <div class="flex items-center justify-between gap-3 mb-2">
          <span class="text-[var(--color-t-muted)]">球面贴图</span>
          <span class="text-xs text-[var(--color-t-muted)]">{{ currentHint }}</span>
        </div>
        <div class="grid grid-cols-4 gap-2">
          <label
            v-for="t in GLOBE_TEXTURES"
            :key="t.id"
            :data-testid="`globe-texture-${t.id}`"
            class="cursor-pointer select-none rounded-lg overflow-hidden border transition"
            :class="globeStyle.style.texture === t.id
              ? 'border-[var(--color-primary-600)] ring-1 ring-[var(--color-primary-600)]'
              : 'border-[var(--color-line)] hover:border-[var(--color-primary-600)]'"
            :title="t.hint"
          >
            <input v-model="globeStyle.style.texture" type="radio" name="globe-texture" :value="t.id" class="sr-only">
            <img :src="globeTextureThumbUrl(t.id)" :alt="t.label" class="w-full block aspect-[2/1] object-cover">
            <div class="px-1.5 py-1 text-xs text-center text-[var(--color-t-muted)]">{{ t.label }}</div>
          </label>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">经纬网</span>
        <div class="checkbox-group">
          <label class="flex items-center gap-1.5 cursor-pointer select-none">
            <input v-model="globeStyle.style.graticules" data-testid="globe-graticules" type="checkbox" class="accent-[var(--color-primary-600)]">
            <span>显示网格</span>
          </label>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">大气光晕</span>
        <div class="checkbox-group">
          <label class="flex items-center gap-1.5 cursor-pointer select-none">
            <input v-model="globeStyle.style.atmosphere" data-testid="globe-atmosphere" type="checkbox" class="accent-[var(--color-primary-600)]">
            <span>显示光晕</span>
          </label>
        </div>
      </div>
      <div class="flex justify-end pt-1">
        <button class="btn btn-sm" @click="globeStyle.reset()">恢复默认</button>
      </div>
    </div>
  </div>
</template>
