/**
 * AbuseIPDB/ipok 纯洁度检查：模块级缓存（bind → 结果），2s 轮询重绘后 tag 保留，
 * 应用内跨页面切换保留（与旧版模块级 Map 语义一致）。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import { bindOf } from '@/utils/format';
import type { PurityInfo } from '@/types/api';

export interface PurityEntry extends PurityInfo {
  status?: 'checking' | 'done' | 'error';
  error?: string;
}

// bind → 检查状态/结果
export const purityCache = reactive(new Map<string, PurityEntry>());

// usageType 英文 → 中文短标签（tag 空间有限；AbuseIPDB + ipok.io 两套词表）
const USAGE_TYPE_SHORT: Record<string, string> = {
  Commercial: '商业',
  Organization: '组织',
  Government: '政府',
  Military: '军事',
  'University/College/School': '学校',
  Library: '图书馆',
  'Content Delivery Network': 'CDN',
  'Fixed Line ISP': '固定ISP',
  'Mobile ISP': '移动ISP',
  'Data Center/Web Hosting/Transit': '数据中心',
  'Search Engine Spider': '爬虫',
  Reserved: '保留',
  hosting: '托管',
  residential: '住宅',
  mobile: '移动',
  business: '企业',
  unknown: '未知',
};

export function usageTypeShort(usage: string): string {
  return USAGE_TYPE_SHORT[usage] || usage || '-';
}

/** 分数着色：0=success，<25=muted，<75=warning，其余 danger */
export function purityScoreClass(score: number): string {
  if (score === 0) return 'badge-success';
  if (score < 25) return 'badge-muted';
  if (score < 75) return 'badge-warning';
  return 'badge-danger';
}

/** 触发检查（幂等：checking 中直接返回）。结果写缓存，tag 由响应式缓存驱动更新。 */
export async function checkPurity(proxy: string): Promise<void> {
  const bind = bindOf(proxy);
  if (purityCache.get(bind)?.status === 'checking') return;
  purityCache.set(bind, { status: 'checking' });
  const toast = useToastStore();
  try {
    const r = await api.purity(bind);
    purityCache.set(bind, { status: 'done', ...r });
  } catch (e) {
    purityCache.set(bind, { status: 'error', error: e instanceof Error ? e.message : String(e) });
    toast.error('纯洁度检查失败', e instanceof Error ? e.message : String(e));
  }
}
