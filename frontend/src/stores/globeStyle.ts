/**
 * 3D 地球显示偏好（纯前端观看偏好，localStorage 持久化，与 theme store 同口径）：
 * 球面贴图 / 经纬网 / 大气光晕。不走后端配置——改完即时生效、不需要重启或落盘。
 *
 * 贴图取 three-globe 官方示例资源（已降采样入库 frontend/public/globe/，离线可用）；
 * 默认「真实地球 + 无网格 + 无光晕」：卡片底色是浅色，深色贴图会显成暗球，
 * 而默认的大气光晕（alpha 混合的 lightskyblue）在浅色底上是一圈蓝雾。
 */
import { defineStore } from 'pinia';
import { reactive, watch } from 'vue';

const STORAGE_KEY = 'proxy-pool-globe-style';

/** 可选球面贴图：id 即 /globe/<id>.jpg；缩略图 /globe/thumb-<id>.jpg */
export const GLOBE_TEXTURES = [
  { id: 'earth-blue-marble', label: '真实地球', hint: 'NASA 蓝色弹珠，浅色底最清楚' },
  { id: 'earth-night', label: '夜景', hint: '深蓝夜空 + 城市灯光' },
  { id: 'earth-day', label: '白昼', hint: '亮蓝海洋 + 绿色陆地' },
  { id: 'earth-dark', label: '暗色', hint: '近黑底，飞线对比度最高' },
] as const;

export type GlobeTextureId = (typeof GLOBE_TEXTURES)[number]['id'];

export interface GlobeStyle {
  texture: GlobeTextureId;
  graticules: boolean;
  atmosphere: boolean;
}

const DEFAULT_STYLE: GlobeStyle = {
  texture: 'earth-blue-marble',
  graticules: false,
  atmosphere: false,
};

export function globeTextureUrl(id: GlobeTextureId): string {
  return `/globe/${id}.jpg`;
}

export function globeTextureThumbUrl(id: GlobeTextureId): string {
  return `/globe/thumb-${id}.jpg`;
}

function isTextureId(v: unknown): v is GlobeTextureId {
  return GLOBE_TEXTURES.some((t) => t.id === v);
}

/** 读持久化偏好：字段逐个校验（localStorage 可能残留旧版/被改坏的值），非法即回默认 */
function readStyle(): GlobeStyle {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<GlobeStyle>;
    return {
      texture: isTextureId(raw.texture) ? raw.texture : DEFAULT_STYLE.texture,
      graticules: typeof raw.graticules === 'boolean' ? raw.graticules : DEFAULT_STYLE.graticules,
      atmosphere: typeof raw.atmosphere === 'boolean' ? raw.atmosphere : DEFAULT_STYLE.atmosphere,
    };
  } catch {
    return { ...DEFAULT_STYLE };
  }
}

export const useGlobeStyleStore = defineStore('globeStyle', () => {
  const style = reactive<GlobeStyle>(readStyle());

  watch(style, () => localStorage.setItem(STORAGE_KEY, JSON.stringify(style)), { deep: true });

  /** 恢复默认样式（设置页「恢复默认」用） */
  function reset(): void {
    Object.assign(style, DEFAULT_STYLE);
  }

  return { style, reset };
});
