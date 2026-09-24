/**
 * IP 纯净度检查：模块级缓存（bind → 结果），2s 轮询重绘后 tag 保留，
 * 应用内跨页面切换保留。
 *
 * 检查源为后端插件（ippure.toml 选定，核心与前端不感知具体源）；
 * 结果字段为通用契约 score（0-100，越高越脏）+ 归一化 usage_type。
 *
 * 状态对齐：后端（PanelTasks）记录检查中间态（checking/error，键为 IP），结果由
 * SQLite 缓存承载（ippure.toml cache_ttl），/api/status 每行下发 purity（缓存结果）+
 * purity_state（中间态）。渲染用 purityEntryOf：后端终态优先（结果/失败），中间态其次
 * （刷新后恢复"检查中"），本地 Map 兜底——防本地 checking 遮蔽后端已完成的终态。
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import { bindOf } from '@/utils/format';
import type { PurityInfo, PurityState } from '@/types/api';

export interface PurityEntry extends PurityInfo {
  status?: 'checking' | 'done' | 'error';
  error?: string;
}

// bind → 检查状态/结果
export const purityCache = reactive(new Map<string, PurityEntry>());

// 归一化 usage_type（核心枚举）→ 中文短标签（tag 空间有限，各源共用一套文案）
// 按"IP 归属者"命名，不用单字或歧义词：hosting 是数据中心（机房），mobile 是移动运营商出口，
// residential 是固网家宽，search_engine 是搜索引擎自有出口
const USAGE_TYPE_SHORT: Record<string, string> = {
  residential: '住宅',
  mobile: '移动网络',
  hosting: '机房',
  business: '企业',
  education: '学校',
  government: '政府',
  military: '军事',
  library: '图书馆',
  cdn: 'CDN',
  search_engine: '搜索引擎',
  reserved: '保留',
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

/**
 * 行内纯净度状态解析（纯函数，可单测）：后端终态优先，本地 Map 兜底。
 * - serverState error → 检查失败（刷新后恢复）
 * - serverState checking → 检查中（刷新后恢复；有旧缓存也显示检查中，用户刚触发）
 * - cached（row.purity，SQLite 缓存结果）→ 已完成结果
 * - 无后端记录 → 本地 Map（乐观/跨路由）
 */
export function purityEntryOf(
  bind: string,
  serverState: PurityState | null | undefined,
  cached: PurityInfo | null | undefined,
): PurityEntry | null | undefined {
  if (serverState?.status === 'error') {
    return { status: 'error', error: serverState.error ?? '检查失败' };
  }
  if (serverState?.status === 'checking') return { status: 'checking' };
  if (cached) return { status: 'done', ...cached };
  return purityCache.get(bind) ?? null;
}

/** 触发检查（幂等：checking 中直接返回）。结果写缓存，tag 由响应式缓存驱动更新。 */
export async function checkPurity(proxy: string): Promise<void> {
  const bind = bindOf(proxy);
  if (purityCache.get(bind)?.status === 'checking') return;
  purityCache.set(bind, { status: 'checking' });
  const toast = useToastStore();
  try {
    const r = await api.purity(bind);
    if ((r as { status?: string }).status === 'checking') {
      // 后端已有该 IP 检查任务在进行（刷新后重复点击/多标签页/同 IP 多端口行）：
      // 保持"检查中"，完成后结果由 SQLite 缓存承载，/api/status 轮询恢复
      purityCache.set(bind, { status: 'checking' });
      return;
    }
    purityCache.set(bind, { status: 'done', ...r });
  } catch (e) {
    purityCache.set(bind, { status: 'error', error: e instanceof Error ? e.message : String(e) });
    toast.error('纯净度检查失败', e instanceof Error ? e.message : String(e));
  }
}
