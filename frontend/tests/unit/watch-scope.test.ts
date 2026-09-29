/**
 * 回归：组件内注册的 watch 必须在卸载后停止（不泄漏）。
 *
 * 背景：GeoMap.vue 曾把两个 watch 写在 `onMounted(async () => { ... await ...; watch(...) })`
 * 的 await 之后。此时组件实例已不在 effect scope 内，Vue 不会把它登记到组件作用域，
 * 卸载后 watcher 仍然存活。依赖 Pinia 单例 store（应用生命周期内不销毁）时，watcher
 * 与其闭包都 GC 不掉——进出该页面 N 次就留下 N 个常驻 watcher，各自重复发请求。
 *
 * 该缺陷不会报错、不改变正常路径行为，只在「反复进出页面」后累积显现，e2e 也测不出，
 * 故用本用例锁住两种写法的差异（setup 顶层 vs onMounted 的 await 之后）。
 */
import { describe, expect, it } from 'vitest';
import { createApp, defineComponent, h, nextTick, onMounted, ref, watch } from 'vue';

const tick = () => new Promise((r) => setTimeout(r, 0));

interface ProbeResult {
  whileMounted: number;
  afterUnmount: number;
}

/**
 * 挂载 → 触发依赖 → 卸载 → 再触发依赖，统计副作用次数。
 *
 * @param register 'setup'      → setup 顶层注册（正确）
 *                 'mountSync'  → onMounted 内同步注册（实测同样正确）
 *                 'afterAwait' → onMounted 内 await 之后注册（缺陷写法）
 */
async function probeWatchScope(register: 'setup' | 'mountSync' | 'afterAwait'): Promise<ProbeResult> {
  const trigger = ref(0);
  const res: ProbeResult = { whileMounted: 0, afterUnmount: 0 };
  let alive = true;

  const register_ = () => {
    watch(
      () => trigger.value,
      () => {
        if (alive) res.whileMounted++;
        else res.afterUnmount++;
      },
    );
  };

  const Child = defineComponent({
    setup() {
      if (register === 'setup') register_();
      else if (register === 'mountSync') onMounted(() => register_());
      else onMounted(async () => {
        await tick();
        register_();
      });
      return () => h('div');
    },
  });

  const app = createApp(Child);
  app.mount(document.createElement('div'));
  await tick(); // 让 afterAwait 分支完成注册

  trigger.value = 1;
  await nextTick();
  trigger.value = 2;
  await nextTick();

  app.unmount();
  alive = false;

  trigger.value = 3;
  await nextTick();
  trigger.value = 4;
  await nextTick();
  await tick();

  return res;
}

describe('watch 的组件作用域归属', () => {
  it('setup 顶层注册：卸载后停止', async () => {
    const r = await probeWatchScope('setup');
    expect(r.whileMounted).toBe(2);
    expect(r.afterUnmount).toBe(0);
  });

  it('onMounted 内同步注册：卸载后停止', async () => {
    const r = await probeWatchScope('mountSync');
    expect(r.whileMounted).toBe(2);
    expect(r.afterUnmount).toBe(0);
  });

  it('onMounted 的 await 之后注册：卸载后仍触发（应避免的写法）', async () => {
    const r = await probeWatchScope('afterAwait');
    // 记录当前 Vue 版本的行为：泄漏即 afterUnmount > 0。
    // 若某次升级后此断言失败（afterUnmount 变 0），说明 Vue 已修正该行为，
    // 届时可放宽——但在此之前，代码里不得出现在 await 之后注册 watch 的写法。
    expect(r.afterUnmount).toBeGreaterThan(0);
  });
});

describe('GeoMap 源码不得在 onMounted 的 await 之后注册 watch', () => {
  it('两个 watch 都写在 setup 顶层', async () => {
    const { readFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    // 用 cwd（vitest 以 frontend/ 为根）定位，避免 jsdom 环境下 import.meta.url 非 file scheme
    const src = readFileSync(resolve(process.cwd(), 'src/components/charts/GeoMap.vue'), 'utf8');
    const script = src.slice(0, src.indexOf('</script>'));
    const onMountedStart = script.indexOf('onMounted(');
    const watches = [...script.matchAll(/watch\(\(\) =>/g)].map((m) => m.index!);

    expect(watches.length).toBeGreaterThan(0);
    expect(onMountedStart).toBeGreaterThan(-1);
    // 每个 watch 都必须出现在 onMounted 块之前（即 setup 顶层）
    for (const at of watches) {
      expect(at).toBeLessThan(onMountedStart);
    }
  });
});
