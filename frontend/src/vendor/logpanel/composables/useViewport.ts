/**
 * 视口度量：布局视口 + 可视视口。
 *
 * 两套度量并存是有意的：
 * - 悬浮几何用布局视口（window.innerWidth/Height）——它不受捏合缩放影响，几何持久化才不会被写坏；
 * - 抽屉高度与键盘内缩用可视视口（visualViewport）——软键盘与浏览器 UI 只改可视视口。
 */
import { onBeforeUnmount, onMounted, reactive } from 'vue';

export interface ViewportMetrics {
  /** 布局视口宽高（px） */
  layoutWidth: number;
  layoutHeight: number;
  /** 可视视口高度与其顶边在布局视口中的偏移（px） */
  visualHeight: number;
  visualTop: number;
}

function read(): ViewportMetrics {
  const visual = window.visualViewport;
  return {
    layoutWidth: window.innerWidth,
    layoutHeight: window.innerHeight,
    visualHeight: visual ? visual.height : window.innerHeight,
    visualTop: visual ? visual.offsetTop : 0,
  };
}

export function useViewport(): ViewportMetrics {
  const metrics = reactive<ViewportMetrics>(read());
  let frame = 0;

  /** 键盘弹出时 visualViewport 的 scroll 逐帧触发，合并到一帧内只重算一次 */
  function refresh(): void {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      Object.assign(metrics, read());
    });
  }

  onMounted(() => {
    const visual = window.visualViewport;
    window.addEventListener('resize', refresh);
    window.addEventListener('orientationchange', refresh);
    visual?.addEventListener('resize', refresh);
    visual?.addEventListener('scroll', refresh);
  });

  onBeforeUnmount(() => {
    const visual = window.visualViewport;
    window.removeEventListener('resize', refresh);
    window.removeEventListener('orientationchange', refresh);
    visual?.removeEventListener('resize', refresh);
    visual?.removeEventListener('scroll', refresh);
    if (frame) cancelAnimationFrame(frame);
  });

  return metrics;
}
