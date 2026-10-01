<script setup lang="ts">
/**
 * 设置页：运行时可变项集中于此，全部经 /api/settings 即刻生效（无配置落盘语义）。
 * 承载 ScheduleSettingsCard（调度参数）与 EnqueueRulesCard（入队筛选规则）。
 */
import { useStatusStore } from '@/stores/status';
import ScheduleSettingsCard from '@/components/ScheduleSettingsCard.vue';
import EnqueueRulesCard from '@/components/EnqueueRulesCard.vue';

const statusStore = useStatusStore();
</script>

<template>
  <ScheduleSettingsCard v-if="statusStore.status" :settings="statusStore.status.settings" />
  <EnqueueRulesCard
    v-if="statusStore.status"
    :rules="statusStore.status.settings.enqueue_rules"
  />
  <div v-if="!statusStore.status" class="card"><div class="card-body text-sm text-[var(--color-t-muted)]">等待状态数据…</div></div>
</template>
