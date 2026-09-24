<script setup lang="ts">
/** 纯净度 tag：分数（0-100，越高越脏，按分值着色）+ 归一化用途类型中文短标签 */
import { computed } from 'vue';
import { purityScoreClass, usageTypeShort, type PurityEntry } from '@/composables/usePurity';

const props = defineProps<{ data: PurityEntry | null | undefined }>();

const visible = computed(() => {
  const d = props.data;
  return !!d && d.status !== 'checking' && d.status !== 'error' && d.score != null;
});
</script>

<template>
  <template v-if="visible">
    <span
      class="badge ml-2"
      :class="purityScoreClass(data!.score!)"
      :title="`纯净度分数 ${data!.score}/100（越高越脏）`"
    >{{ data!.score }}</span>
    <span class="badge badge-info ml-1" :title="data!.usage_type || ''">
      {{ usageTypeShort(data!.usage_type || '') }}
    </span>
  </template>
</template>
