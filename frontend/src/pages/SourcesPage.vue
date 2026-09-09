<script setup lang="ts">
/** 代理源页：源列表、状态、暂停/恢复、手动拉取。数据来自 statusStore.sources。 */
import { computed, ref } from 'vue';
import { api } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { useToastStore } from '@/stores/toast';
import { fmtDuration } from '@/utils/format';

const store = useStatusStore();
const toast = useToastStore();

const sources = computed(() => store.status?.sources ?? []);
const fetching = ref(false);

async function doFetch() {
  fetching.value = true;
  try {
    const d = await api.fetch();
    toast.success('拉取完成', `可用代理 ${d.available}`);
  } catch (e) {
    toast.error('拉取失败', e instanceof Error ? e.message : String(e));
  } finally {
    fetching.value = false;
  }
}

async function doPause(source: string) {
  try {
    await api.pauseSource(source);
    toast.success('已暂停', source);
  } catch (e) {
    toast.error('暂停失败', e instanceof Error ? e.message : String(e));
  }
}

async function doResume(source: string) {
  try {
    await api.resumeSource(source);
    toast.success('已恢复', source);
  } catch (e) {
    toast.error('恢复失败', e instanceof Error ? e.message : String(e));
  }
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
        <button data-testid="src-fetch" class="btn btn-primary btn-sm" :disabled="fetching" @click="doFetch">
          {{ fetching ? '处理中…' : '拉取代理' }}
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
              <td class="text-right">
                <template v-if="s.enabled">
                  <button v-if="s.paused" class="btn btn-ghost btn-sm" data-action="resume" :data-source="s.name" @click="doResume(s.name)">恢复</button>
                  <button v-else class="btn btn-ghost btn-sm" data-action="pause" :data-source="s.name" @click="doPause(s.name)">暂停</button>
                </template>
                <template v-else>-</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
