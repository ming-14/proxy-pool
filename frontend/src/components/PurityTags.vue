<script setup lang="ts">
/** 纯洁度 tag：滥用置信度分数（按分值着色）+ usageType 中文短标签 */
import { computed } from 'vue';
import { purityScoreClass, usageTypeShort, type PurityEntry } from '@/composables/usePurity';

const props = defineProps<{ data: PurityEntry | null | undefined }>();

const visible = computed(() => {
  const d = props.data;
  return !!d && d.status !== 'checking' && d.status !== 'error' && d.abuse_confidence_score != null;
});
</script>

<template>
  <template v-if="visible">
    <span
      class="badge ml-2"
      :class="purityScoreClass(data!.abuse_confidence_score!)"
      :title="`滥用置信度 ${data!.abuse_confidence_score}/100`"
    >{{ data!.abuse_confidence_score }}</span>
    <span class="badge badge-info ml-1" :title="data!.usage_type || ''">
      {{ usageTypeShort(data!.usage_type || '') }}
    </span>
  </template>
</template>
