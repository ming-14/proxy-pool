<script setup lang="ts">
/**
 * 设置页：运行时可变项集中于此，全部经 /api/settings 即刻生效（无配置落盘语义）。
 * 承载 ScheduleSettingsCard（8 项调度参数/允许类型/探测 URL 等）。
 */
import { useStatusStore } from '@/stores/status';
import ScheduleSettingsCard from '@/components/ScheduleSettingsCard.vue';

const statusStore = useStatusStore();
</script>

<template>
  <ScheduleSettingsCard v-if="statusStore.status" :settings="statusStore.status.settings" />
  <div v-else class="card"><div class="card-body text-sm text-[var(--color-t-muted)]">等待状态数据…</div></div>
</template>
