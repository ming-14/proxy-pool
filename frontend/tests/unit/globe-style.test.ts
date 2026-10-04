/** globeStyle store：默认值、脏数据回退、持久化与恢复默认。 */
import { beforeEach, describe, expect, it } from 'vitest';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { globeTextureThumbUrl, globeTextureUrl, useGlobeStyleStore } from '@/stores/globeStyle';

const KEY = 'proxy-pool-globe-style';

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

describe('useGlobeStyleStore', () => {
  it('无持久化数据时用默认样式（真实地球 + 无网格 + 无光晕）', () => {
    const { style } = useGlobeStyleStore();
    expect(style).toEqual({ texture: 'earth-blue-marble', graticules: false, atmosphere: false });
  });

  it('采纳持久化中的合法值', () => {
    localStorage.setItem(KEY, JSON.stringify({ texture: 'earth-night', graticules: true, atmosphere: true }));
    const { style } = useGlobeStyleStore();
    expect(style).toEqual({ texture: 'earth-night', graticules: true, atmosphere: true });
  });

  it('非法贴图 id / 非布尔开关逐字段回默认，坏 JSON 整体回默认', () => {
    localStorage.setItem(KEY, JSON.stringify({ texture: 'earth-mars', graticules: 'yes' }));
    expect(useGlobeStyleStore().style).toEqual({
      texture: 'earth-blue-marble',
      graticules: false,
      atmosphere: false,
    });

    setActivePinia(createPinia());
    localStorage.setItem(KEY, '{ 坏 JSON');
    expect(useGlobeStyleStore().style.texture).toBe('earth-blue-marble');
  });

  it('改动写入 localStorage，reset 回默认并落盘', async () => {
    const store = useGlobeStyleStore();
    store.style.texture = 'earth-dark';
    store.style.graticules = true;
    await nextTick(); // 等 watch（flush: pre）落盘
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({
      texture: 'earth-dark',
      graticules: true,
      atmosphere: false,
    });

    store.reset();
    await nextTick();
    expect(store.style.texture).toBe('earth-blue-marble');
    expect(JSON.parse(localStorage.getItem(KEY)!).graticules).toBe(false);
  });

  it('贴图/缩略图 URL 与 id 对应', () => {
    expect(globeTextureUrl('earth-night')).toBe('/globe/earth-night.jpg');
    expect(globeTextureThumbUrl('earth-night')).toBe('/globe/thumb-earth-night.jpg');
  });
});
