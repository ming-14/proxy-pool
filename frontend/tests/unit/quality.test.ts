/** 低质量标记：阈值判定与命中原因（面板色条 + 原因标签的数据源） */
import { describe, expect, it } from 'vitest';
import { QUALITY_THRESHOLDS, hasQualityIssue, qualityFlags } from '@/utils/quality';
import type { StandbyEntry } from '@/types/api';

function entry(over: Partial<StandbyEntry> = {}): StandbyEntry {
  return {
    proxy: 'http://1.2.3.4:8080',
    latency: 0.2,
    consecutive_fails: 0,
    pinned: false,
    locked: false,
    purity: null,
    protocol: 'http',
    joined_at: null,
    ...over,
  };
}

describe('qualityFlags', () => {
  it('健康代理不命中任何标记', () => {
    const f = qualityFlags(entry(), 100);
    expect(hasQualityIssue(f)).toBe(false);
  });

  it('连续失败计为 failing', () => {
    expect(qualityFlags(entry({ consecutive_fails: 1 }), 100).failing).toBe(true);
  });

  it('延迟超过阈值计为 slow（等于阈值不算）', () => {
    expect(qualityFlags(entry({ latency: QUALITY_THRESHOLDS.slow_latency }), 100).slow).toBe(false);
    expect(qualityFlags(entry({ latency: QUALITY_THRESHOLDS.slow_latency + 0.01 }), 100).slow).toBe(true);
  });

  it('成功率低于阈值计为 unstable（等于阈值不算）', () => {
    expect(qualityFlags(entry(), QUALITY_THRESHOLDS.min_success_rate).unstable).toBe(false);
    expect(qualityFlags(entry(), QUALITY_THRESHOLDS.min_success_rate - 1).unstable).toBe(true);
  });

  it('无探测（成功率 null）不判为不稳定，也不误判为健康失败', () => {
    expect(qualityFlags(entry(), null).unstable).toBe(false);
  });

  it('滥用置信度达到阈值计为 dirty', () => {
    expect(qualityFlags(entry({ purity: { abuse_confidence_score: QUALITY_THRESHOLDS.max_abuse_score } }), 100).dirty).toBe(true);
    expect(qualityFlags(entry({ purity: { abuse_confidence_score: 0 } }), 100).dirty).toBe(false);
  });

  it('未检查纯洁度（无缓存）不判为 dirty', () => {
    expect(qualityFlags(entry({ purity: null }), 100).dirty).toBe(false);
  });

  it('未探测过延迟（latency null）不判为 slow', () => {
    expect(qualityFlags(entry({ latency: null }), 100).slow).toBe(false);
  });
});
