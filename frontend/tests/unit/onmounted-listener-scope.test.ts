/**
 * 回归：组件在「await 期间被卸载」时，副作用不能被登记而无人回收。
 *
 * 背景：GeoMap.vue 曾在 `onMounted(async () => { ...; await refresh(true); ...;
 * window.addEventListener('resize', handler) })` 里注册窗口监听——handler 在 await
 * 之后才创建，若组件在 await 期间卸载，onBeforeUnmount 拿到的 handler 仍是 null，
 * 摘不掉、监听器残留在 window 上（每次进出该页多一个 resize 回调，且闭包持有已销毁
 * 的 chart）。修法是改用 ResizeObserver 并在同步阶段建立：它必然能被 disconnect 回收。
 *
 * 本用例锁两件事：① 这种"await 后注册"的写法确实会残留（防止再写回去）；
 * ② GeoMap 的 resize 处理不依赖 window.addEventListener('resize')。
 */
import { describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, onBeforeUnmount, onMounted } from 'vue';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

describe('await 之后注册的 window 监听器会在卸载时残留', () => {
  it('延迟注册（await 期间卸载）→ 监听器被登记且无人摘除', async () => {
    const added = vi.spyOn(window, 'addEventListener');
    const removed = vi.spyOn(window, 'removeEventListener');

    let resolveGate!: () => void;
    const gate = new Promise<void>((r) => {
      resolveGate = r;
    });
    let handler: (() => void) | null = null;

    const Child = defineComponent({
      setup() {
        onMounted(async () => {
          await gate;
          handler = () => {};
          window.addEventListener('resize', handler);
        });
        onBeforeUnmount(() => {
          if (handler) window.removeEventListener('resize', handler);
        });
        return () => h('div');
      },
    });

    const app = createApp(Child);
    app.mount(document.createElement('div'));
    await Promise.resolve();

    app.unmount(); // await 未完成即卸载
    resolveGate();
    await new Promise((r) => setTimeout(r, 0));

    expect(handler).not.toBeNull();
    expect(added).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(removed).not.toHaveBeenCalledWith('resize', expect.any(Function));

    added.mockRestore();
    removed.mockRestore();
  });
});

describe('GeoMap 的尺寸自适应不依赖 window resize 监听', () => {
  it('源码用 ResizeObserver（同步建立、可被 disconnect 回收）', () => {
    const src = readFileSync(resolve(process.cwd(), 'src/components/charts/GeoMap.vue'), 'utf8');
    const script = src.slice(0, src.indexOf('</script>'));

    // 不得再出现 window resize 监听（它在 await 之后注册会残留）
    expect(script).not.toContain("window.addEventListener('resize'");
    // 必须有 RO，且在 onBeforeUnmount 里 disconnect
    expect(script).toContain('ResizeObserver');
    expect(script).toContain('ro?.disconnect()');
  });
});
