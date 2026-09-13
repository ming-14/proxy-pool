<script setup lang="ts">
/**
 * 设置页：顶部分区 Tab（仅运行时可变项，无配置落盘语义）。
 *   - 代理服务：调度参数 8 项（interval/concurrency/timeout/salgorithm/probe_url/
 *     allowed_types/fill 倍率），经 /api/settings 即刻生效（由 ScheduleSettingsCard 承载）；
 *   - 界面偏好：主题、轮询间隔、历史保留点数（localStorage，仅前端生效）；
 *   - 数据维护：清空面板日志、立即刷新状态等动作。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import { useStatusStore } from '@/stores/status';
import { useThemeStore } from '@/stores/theme';
import { useToastStore } from '@/stores/toast';
import { usePrefsStore, DEFAULT_POLL_MS, DEFAULT_HISTORY_LIMIT } from '@/stores/prefs';
import ScheduleSettingsCard from '@/components/ScheduleSettingsCard.vue';

type TabKey = 'service' | 'prefs' | 'data';
const TABS: { key: TabKey; label: string }[] = [
  { key: 'service', label: '代理服务' },
  { key: 'prefs', label: '界面偏好' },
  { key: 'data', label: '数据维护' },
];

const tab = ref<TabKey>('service');

const statusStore = useStatusStore();
const themeStore = useThemeStore();
const toast = useToastStore();
const prefsStore = usePrefsStore();

// ---------------- 界面偏好 ----------------
const form = reactive({
  pollMs: prefsStore.pollIntervalMs,
  historyLimit: prefsStore.historyLimit,
});
const savingPrefs = ref(false);

// store 值变化时（含恢复默认）同步表单；编辑中保留用户输入
watch(
  () => [prefsStore.pollIntervalMs, prefsStore.historyLimit],
  ([p, h]) => {
    if (savingPrefs.value) return;
    form.pollMs = p;
    form.historyLimit = h;
  },
);

const prefsDirty = computed(() => form.pollMs !== prefsStore.pollIntervalMs || form.historyLimit !== prefsStore.historyLimit);

async function savePrefs() {
  if (!Number.isFinite(form.pollMs) || !Number.isInteger(form.pollMs) || form.pollMs < 500 || form.pollMs > 60000) {
    toast.error('参数错误', '轮询间隔必须在 500–60000ms 之间');
    return;
  }
  if (!Number.isFinite(form.historyLimit) || !Number.isInteger(form.historyLimit) || form.historyLimit < 200 || form.historyLimit > 20000) {
    toast.error('参数错误', '历史保留点数必须在 200–20000 之间');
    return;
  }
  savingPrefs.value = true;
  try {
    prefsStore.pollIntervalMs = form.pollMs;
    prefsStore.historyLimit = form.historyLimit;
    prefsStore.apply();
    toast.success('已保存', '偏好仅本机生效（localStorage）');
  } finally {
    savingPrefs.value = false;
  }
}

function resetPrefs() {
  savingPrefs.value = true;
  prefsStore.reset();
  form.pollMs = DEFAULT_POLL_MS;
  form.historyLimit = DEFAULT_HISTORY_LIMIT;
  savingPrefs.value = false;
  toast.success('已复原', '已恢复默认偏好');
}

// ---------------- 数据维护 ----------------
async function clearLogs() {
  try {
    await api.clearLogs();
    toast.success('已清空', '面板日志镜像已清空（不影响 stderr/文件日志）');
  } catch (e) {
    toast.error('清空失败', e instanceof Error ? e.message : String(e));
  }
}

async function refreshNow() {
  await statusStore.tick();
  toast.success(
    statusStore.connected ? '已刷新' : '刷新失败',
    statusStore.connected ? '已从 /api/status 取回最新数据' : '服务端不可达',
  );
}
</script>

<template>
  <div class="space-y-5">
    <!-- Tab 栏 -->
    <div class="flex gap-1 rounded-lg bg-[var(--color-hover)] p-1 w-fit">
      <button
        v-for="t in TABS"
        :key="t.key"
        :data-testid="`settings-tab-${t.key}`"
        class="px-4 py-1.5 rounded-md text-sm font-medium transition-colors"
        :class="tab === t.key
          ? 'bg-[var(--color-card)] text-[var(--color-primary-600)] shadow-sm'
          : 'text-[var(--color-t-secondary)] hover:text-[var(--color-t-primary)]'"
        @click="tab = t.key"
      >{{ t.label }}</button>
    </div>

    <!-- 代理服务：调度参数（即刻生效） -->
    <section v-show="tab === 'service'">
      <ScheduleSettingsCard v-if="statusStore.status" :settings="statusStore.status.settings" />
      <div v-else class="card"><div class="card-body text-sm text-[var(--color-t-muted)]">等待状态数据…</div></div>
    </section>

    <!-- 界面偏好 -->
    <section v-show="tab === 'prefs'" class="card">
      <div class="card-header"><div class="card-title">界面偏好</div></div>
      <div class="card-body space-y-3 text-sm">
        <div class="flex items-center justify-between gap-3">
          <span class="text-[var(--color-t-muted)]">主题</span>
          <div class="flex items-center gap-4">
            <label class="flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="radio"
                name="pref-theme"
                value="light"
                class="accent-[var(--color-primary-600)]"
                :checked="themeStore.theme === 'light'"
                @change="themeStore.theme = 'light'"
              >
              <span>浅色</span>
            </label>
            <label class="flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="radio"
                name="pref-theme"
                value="dark"
                class="accent-[var(--color-primary-600)]"
                :checked="themeStore.theme === 'dark'"
                @change="themeStore.theme = 'dark'"
              >
              <span>深色</span>
            </label>
          </div>
        </div>
        <div class="flex items-center justify-between gap-3">
          <span class="text-[var(--color-t-muted)]">轮询间隔</span>
          <div class="flex items-center gap-1">
            <input v-model.number="form.pollMs" data-testid="pref-poll" type="number" class="input w-24 text-right" min="500" step="500">
            <span class="text-[var(--color-t-muted)] text-xs">ms</span>
          </div>
        </div>
        <div class="flex items-center justify-between gap-3">
          <span class="text-[var(--color-t-muted)]">历史保留点数</span>
          <div class="flex items-center gap-1">
            <input v-model.number="form.historyLimit" data-testid="pref-history" type="number" class="input w-24 text-right" min="200" step="100">
            <span class="text-[var(--color-t-muted)] text-xs">点</span>
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button data-testid="pref-reset" class="btn btn-ghost btn-sm" :disabled="savingPrefs" @click="resetPrefs">复原默认</button>
          <button data-testid="pref-save" class="btn btn-primary btn-sm" :disabled="!prefsDirty || savingPrefs" @click="savePrefs">
            {{ savingPrefs ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </section>

    <!-- 数据维护 -->
    <section v-show="tab === 'data'">
      <div class="card">
        <div class="card-header"><div class="card-title">清空面板日志</div></div>
        <div class="card-body flex items-center justify-between gap-3 text-sm">
          <span class="text-[var(--color-t-muted)]">仅清空面板日志镜像，不影响 stderr 与文件日志</span>
          <button data-testid="data-clear-logs" class="btn btn-danger btn-sm" @click="clearLogs">清空</button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">立即刷新</div></div>
        <div class="card-body flex items-center justify-between gap-3 text-sm">
          <span class="text-[var(--color-t-muted)]">立即拉取一次 /api/status，不等下一轮轮询</span>
          <button data-testid="data-refresh" class="btn btn-ghost btn-sm" @click="refreshNow">刷新</button>
        </div>
      </div>
    </section>
  </div>
</template>