/**
 * 跟随意图复核：判定一次滚动之后是否应认定「用户上滚」而关闭跟随。
 *
 * 程序侧贴底与测高修正都会让位置短暂落后于末尾——库的落点要到后续帧才对齐，测高每批
 * resizeItem 又改总高——此刻「不在末尾」并不等于用户上滚。这段时序在无头浏览器里不可复现，
 * 故把判定提成纯函数，正确性由单测保证（与 utils/sheet.ts 同一做法）。
 */

export type FollowVerdict = 'keep' | 'drop';

/** 复核状态：上次复核时的位置 + 连续「位置未推进」的复核次数 */
export interface FollowState {
  prevTop: number;
  stagnantChecks: number;
}

export interface FollowStep {
  verdict: FollowVerdict;
  state: FollowState;
}

/**
 * 单次复核。
 *
 * @param lag                距末尾的距离（scrollHeight - scrollTop - clientHeight）
 * @param top                当前 scrollTop
 * @param threshold          贴底判定阈值
 * @param maxStagnantChecks  允许连续「位置未推进」的复核次数，即留给程序侧追赶的帧数
 */
export function followStep(
  state: FollowState,
  lag: number,
  top: number,
  threshold: number,
  maxStagnantChecks: number,
): FollowStep {
  if (lag <= threshold) return { verdict: 'keep', state: { prevTop: top, stagnantChecks: 0 } };
  // 仍在朝末尾推进：程序侧贴底/测高修正还在追赶，不是用户上滚
  if (top > state.prevTop) return { verdict: 'keep', state: { prevTop: top, stagnantChecks: 0 } };
  // 视口上移：用户上滚，立即停跟随
  if (top < state.prevTop) return { verdict: 'drop', state: { prevTop: top, stagnantChecks: 0 } };
  // 停住不动：先给追赶留出若干帧；连续停够次数仍未到末尾才判定用户把视口按在了别处
  const stagnantChecks = state.stagnantChecks + 1;
  return {
    verdict: stagnantChecks >= maxStagnantChecks ? 'drop' : 'keep',
    state: { prevTop: top, stagnantChecks },
  };
}
