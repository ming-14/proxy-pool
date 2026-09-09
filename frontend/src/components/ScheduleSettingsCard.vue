<script setup lang="ts">
/**
 * 调度参数卡片：编辑区本地状态 + 脏检测（保存按钮启停），
 * 未编辑时跟随 status.settings 同步；旧版"焦点在编辑区跳过重绘"补丁不再需要。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import type { ScheduleSettings } from '@/types/api';

const props = defineProps<{ settings: ScheduleSettings }>();
const toast = useToastStore();

const ALG_LABELS: Record<ScheduleSettings['salgorithm'], string> = {
  fa: '首个可用 (fa)',
  rr: '轮询 (rr)',
  rc: '随机 (rc)',
  lc: '最少连接 (lc)',
};

const form = reactive({
  interval: props.settings.interval,
  concurrency: props.settings.concurrency,
  timeout: props.settings.timeout,
  salgorithm: props.settings.salgorithm,
  probe_url: props.settings.probe_url,
});
const baseline = ref({ ...form });
const saving = ref(false);

// 未编辑时跟随服务端值；编辑中不覆盖用户输入
watch(
  () => props.settings,
  (s) => {
    if (dirty.value) return;
    form.interval = s.interval;
    form.concurrency = s.concurrency;
    form.timeout = s.timeout;
    form.salgorithm = s.salgorithm;
    form.probe_url = s.probe_url;
    baseline.value = { ...form };
  },
);

const dirty = computed(
  () =>
    form.interval !== baseline.value.interval ||
    form.concurrency !== baseline.value.concurrency ||
    form.timeout !== baseline.value.timeout ||
    form.salgorithm !== baseline.value.salgorithm ||
    form.probe_url.trim() !== baseline.value.probe_url,
);

async function save() {
  if (!Number.isFinite(form.interval) || !Number.isInteger(form.interval) || form.interval < 1) {
    toast.error('参数错误', '心跳间隔必须为正整数');
    return;
  }
  if (!Number.isFinite(form.concurrency) || !Number.isInteger(form.concurrency) || form.concurrency < 1) {
    toast.error('参数错误', '并发必须为正整数');
    return;
  }
  if (!Number.isFinite(form.timeout) || form.timeout <= 0) {
    toast.error('参数错误', '超时必须为正数');
    return;
  }
  if (!form.probe_url.trim()) {
    toast.error('参数错误', '探测 URL 不能为空');
    return;
  }
  saving.value = true;
  try {
    const d = await api.settings({
      interval: form.interval,
      concurrency: form.concurrency,
      timeout: form.timeout,
      salgorithm: form.salgorithm,
      probe_url: form.probe_url.trim(),
    });
    toast.success('已更新', '调度参数即刻生效');
    baseline.value = {
      interval: d.settings.interval,
      concurrency: d.settings.concurrency,
      timeout: d.settings.timeout,
      salgorithm: d.settings.salgorithm,
      probe_url: d.settings.probe_url,
    };
  } catch (e) {
    toast.error('保存失败', e instanceof Error ? e.message : String(e));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div data-testid="settings-card" class="card">
    <div class="card-header"><div class="card-title">调度参数</div></div>
    <div class="card-body space-y-3 text-sm">
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">心跳间隔</span>
        <div class="flex items-center gap-1">
          <input v-model.number="form.interval" data-testid="set-interval" type="number" class="input w-20 text-right" min="1" step="1">
          <span class="text-[var(--color-t-muted)] text-xs">s</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">并发</span>
        <div class="flex items-center gap-1">
          <input v-model.number="form.concurrency" type="number" class="input w-20 text-right" min="1" step="1">
          <span class="text-[var(--color-t-muted)] text-xs">x</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">超时</span>
        <div class="flex items-center gap-1">
          <input v-model.number="form.timeout" type="number" class="input w-20 text-right" min="0.5" step="0.5">
          <span class="text-[var(--color-t-muted)] text-xs">s</span>
        </div>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">调度算法</span>
        <select v-model="form.salgorithm" class="select w-40">
          <option v-for="(label, k) in ALG_LABELS" :key="k" :value="k">{{ label }}</option>
        </select>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">允许类型</span>
        <span class="font-medium text-[var(--color-t-muted)]">{{ props.settings.allowed_types.join(', ') || '-' }}</span>
      </div>
      <div class="flex items-center justify-between gap-3">
        <span class="text-[var(--color-t-muted)]">探测 URL</span>
        <input v-model="form.probe_url" data-testid="set-probe-url" type="text" class="input w-48">
      </div>
      <div class="flex justify-end pt-1">
        <button data-testid="set-save" class="btn btn-primary btn-sm" :disabled="!dirty || saving" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
