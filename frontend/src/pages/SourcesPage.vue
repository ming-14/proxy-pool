<script setup lang="ts">
/**
 * 代理源页：源列表、状态、暂停/恢复、全量与单源拉取。数据来自 statusStore.sources。
 * 单源拉取按钮只对启用且未暂停的源显示（暂停态下该源本就拉不动）。
 * 「处理中」以后端下发的 fetching 为准（刷新/重开页面仍可恢复），本地 pending 只补
 * 点击到下一次轮询之间的空档；后端同一时刻只放行一个拉取，故任一拉取进行中就统一
 * 禁用全部拉取按钮。
 */
import { computed, ref } from 'vue';
import { api } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { runAction, type ActionToast } from '@/composables/runAction';
import type { SourceStat } from '@/types/api';
import { fmtDuration } from '@/utils/format';

const store = useStatusStore();

const sources = computed(() => store.status?.sources ?? []);
/** 本地即时反馈（后端状态要等下一次轮询才可见） */
const pending = ref(new Set<string>());
const pendingAll = ref(false);

const fullFetching = computed(() => (store.status?.fetching_all ?? false) || pendingAll.value);
const anyFetching = computed(() =>
  fullFetching.value || pending.value.size > 0 || sources.value.some((s) => s.fetching),
);
/** 闸门被占用时禁用的按钮要给悬停说明，否则点了没反应却看不出原因 */
const busyHint = computed(() => (anyFetching.value ? '已有拉取任务在跑' : undefined));

/** 该源按钮是否显示「处理中…」：后端状态或本地刚点击 */
function rowFetching(s: SourceStat): boolean {
  return s.fetching || pending.value.has(s.name);
}

/** 拉取响应 → toast：后端幂等拒绝时按「进行中」提示，不谎报完成 */
function fetchToast(
  d: Awaited<ReturnType<typeof api.fetch>>,
  done: (available: number) => string,
): ActionToast {
  if ('status' in d) return { type: 'info', title: '拉取进行中', message: '已有拉取任务在跑，请稍候' };
  return { title: '拉取完成', message: done(d.available) };
}

async function doFetch() {
  pendingAll.value = true;
  await runAction(() => api.fetch(), {
    ok: (d) => fetchToast(d, (n) => `可用代理 ${n}`),
    failTitle: '拉取失败',
    after: async () => {
      pendingAll.value = false;
      await store.refreshNow();
    },
  });
}

/** 单源拉取：拉取可能触发去重率暂停，完成后立即刷新以同步暂停态与按钮可见性 */
async function doFetchSource(s: SourceStat) {
  pending.value.add(s.name);
  await runAction(() => api.fetch(s.name), {
    ok: (d) => fetchToast(d, (n) => `${s.short_name} · 可用代理 ${n}`),
    failTitle: '拉取失败',
    after: async () => {
      pending.value.delete(s.name);
      await store.refreshNow();
    },
  });
}

async function doPause(source: string) {
  await runAction(() => api.pauseSource(source), {
    ok: { title: '已暂停', message: source },
    failTitle: '暂停失败',
    after: () => store.refreshNow(),
  });
}

async function doResume(source: string) {
  await runAction(() => api.resumeSource(source), {
    ok: { title: '已恢复', message: source },
    failTitle: '恢复失败',
    after: () => store.refreshNow(),
  });
}
</script>

<template>
  <div class="space-y-5">
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">代理源</div>
          <div class="card-subtitle">建议及时更新</div>
        </div>
        <button data-testid="src-fetch" class="btn btn-primary btn-sm" :disabled="anyFetching" :title="busyHint" @click="doFetch">
          {{ fullFetching ? '处理中…' : '拉取代理' }}
        </button>
      </div>
      <div data-testid="src-table" class="table-wrap border-0 rounded-t-none">
        <div v-if="!sources.length" class="empty-state">未配置代理源</div>
        <table v-else>
          <thead>
            <tr><th>源</th><th>状态</th><th>暂停剩余</th><th class="text-right">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in sources" :key="s.name" :class="{ 'row-paused': s.paused }">
              <td>
                <code>{{ s.short_name }}</code>
                <div class="text-xs text-[var(--color-t-muted)] mt-0.5">{{ s.name }}</div>
              </td>
              <td>
                <span v-if="!s.enabled" class="badge badge-muted">禁用</span>
                <span v-else-if="s.paused" class="badge badge-warning">已暂停</span>
                <span v-else class="badge badge-success">正常</span>
              </td>
              <td class="text-[var(--color-t-muted)]">{{ s.pause_remaining != null ? fmtDuration(s.pause_remaining) : '-' }}</td>
              <td>
                <div class="flex items-center justify-end gap-2">
                  <template v-if="s.enabled && s.paused">
                    <button class="btn btn-ghost btn-sm" data-action="resume" :data-source="s.name" @click="doResume(s.name)">恢复</button>
                  </template>
                  <template v-else-if="s.enabled">
                    <button class="btn btn-ghost btn-sm" data-action="pause" :data-source="s.name" @click="doPause(s.name)">暂停</button>
                    <button
                      class="btn btn-ghost btn-sm"
                      data-action="fetch"
                      :data-source="s.name"
                      :disabled="anyFetching"
                      :title="busyHint"
                      @click="doFetchSource(s)"
                    >
                      {{ rowFetching(s) ? '处理中…' : '拉取' }}
                    </button>
                  </template>
                  <template v-else>-</template>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
