/**
 * 队列页低质量标记：把"值得关注"的代理挑出来（行下质量带模式的横向比较补偿）。
 *
 * 纯函数，阈值集中在此，面板只按返回的标记加色条与底色；命中任一即视为需关注。
 */
import type { StandbyEntry } from '@/types/api';

export const QUALITY_THRESHOLDS = {
  /** 延迟超过该值（秒）视为偏慢 */
  slow_latency: 1.0,
  /** 窗口内探测成功率低于该值（%）视为不稳定 */
  min_success_rate: 80,
  /** 滥用置信度达到该值（0-100）视为不干净 */
  max_abuse_score: 50,
};

export interface QualityFlags {
  /** 有连续失败（心跳/补测未成功） */
  failing: boolean;
  /** 延迟偏慢 */
  slow: boolean;
  /** 窗口内成功率偏低 */
  unstable: boolean;
  /** 滥用置信度偏高 */
  dirty: boolean;
}

/** 命中任一标记（面板据此加高亮） */
export function hasQualityIssue(flags: QualityFlags): boolean {
  return flags.failing || flags.slow || flags.unstable || flags.dirty;
}

/** 计算低质量标记；successRate 为 null（窗口内无探测）时不计入不稳定 */
export function qualityFlags(
  entry: Pick<StandbyEntry, 'consecutive_fails' | 'latency' | 'purity'>,
  successRate: number | null,
): QualityFlags {
  const score = entry.purity?.abuse_confidence_score;
  return {
    failing: entry.consecutive_fails > 0,
    slow: entry.latency !== null && entry.latency > QUALITY_THRESHOLDS.slow_latency,
    unstable: successRate !== null && successRate < QUALITY_THRESHOLDS.min_success_rate,
    dirty: typeof score === 'number' && score >= QUALITY_THRESHOLDS.max_abuse_score,
  };
}
