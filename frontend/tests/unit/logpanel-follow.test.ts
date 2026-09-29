/**
 * 跟随意图复核单测：程序侧贴底 / 测高修正造成的「暂时不在末尾」不得被误判成用户上滚。
 *
 * 这段时序由 rAF 与库内部对齐决定，无头浏览器里不可复现，故判定的正确性落在纯函数上
 * （与 logpanel-sheet.test.ts 对抽屉几何的做法一致）。
 */
import { describe, expect, it } from 'vitest';
import { followStep, type FollowState } from '@/vendor/logpanel/utils/follow';

const THRESHOLD = 5;
const MAX_STAGNANT = 5;

function step(state: FollowState, lag: number, top: number) {
  return followStep(state, lag, top, THRESHOLD, MAX_STAGNANT);
}

describe('followStep', () => {
  it('已贴底：维持跟随并清零停住计数', () => {
    const { verdict, state } = step({ prevTop: 100, stagnantChecks: 3 }, 0, 100);
    expect(verdict).toBe('keep');
    expect(state).toEqual({ prevTop: 100, stagnantChecks: 0 });
  });

  it('位置仍朝末尾推进：程序侧在追赶，不得判成上滚', () => {
    // 距末尾 400px，但比上次复核更靠后 ⇒ 内容变高后的贴底/测高修正尚未落定
    const { verdict, state } = step({ prevTop: 100, stagnantChecks: 4 }, 400, 160);
    expect(verdict).toBe('keep');
    expect(state).toEqual({ prevTop: 160, stagnantChecks: 0 });
  });

  it('视口上移：用户上滚，立即停跟随', () => {
    expect(step({ prevTop: 400, stagnantChecks: 0 }, 900, 300).verdict).toBe('drop');
  });

  it('位置停住但未超容忍帧数：继续等程序侧追赶', () => {
    let state: FollowState = { prevTop: 200, stagnantChecks: 0 };
    for (let i = 1; i < MAX_STAGNANT; i++) {
      const result = step(state, 300, 200);
      expect(result.verdict).toBe('keep');
      expect(result.state.stagnantChecks).toBe(i);
      state = result.state;
    }
  });

  it('停够容忍帧数仍不在末尾：判定用户把视口按在了别处', () => {
    expect(step({ prevTop: 200, stagnantChecks: MAX_STAGNANT - 1 }, 300, 200).verdict).toBe('drop');
  });

  it('推进过就清零：追赶期间的停顿时不累积成误判', () => {
    let state = step({ prevTop: 100, stagnantChecks: 3 }, 500, 120).state;
    expect(state.stagnantChecks).toBe(0);
    state = step(state, 400, 140).state;
    expect(state.stagnantChecks).toBe(0);
  });
});
