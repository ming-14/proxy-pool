<script setup lang="ts">
/**
 * 入队筛选规则卡片：按配置的指标逐条启停 + 改阈值（优先级只读，以执行顺序一行展示），
 * 未编辑时跟随 status.settings.enqueue_rules 同步；保存经 /api/settings 即刻生效。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api, errText } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import type { EnqueueRulePatch, EnqueueRuleSetting } from '@/types/api';

const props = defineProps<{ rules: EnqueueRuleSetting[] }>();
const toast = useToastStore();

interface RuleForm {
  metric: string;
  label: string;
  priority: number;
  enabled: boolean;
  max_latency: number;
  allowText: string;
  max_score: number;
  min_download_mbps: number;
}

function fromSetting(rule: EnqueueRuleSetting): RuleForm {
  const allow = Array.isArray(rule.params.allow) ? (rule.params.allow as string[]) : [];
  return {
    metric: rule.metric,
    label: rule.label,
    priority: rule.priority,
    enabled: rule.enabled,
    max_latency: Number(rule.params.max_latency ?? 0),
    allowText: allow.join(', '),
    max_score: Number(rule.params.max_score ?? 0),
    min_download_mbps: Number(rule.params.min_download_mbps ?? 0),
  };
}

function parseAllow(text: string): string[] {
  return text.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
}

function toPatchItem(r: RuleForm): EnqueueRulePatch {
  if (r.metric === 'latency') return { metric: r.metric, enabled: r.enabled, max_latency: r.max_latency };
  if (r.metric === 'country') return { metric: r.metric, enabled: r.enabled, allow: parseAllow(r.allowText) };
  if (r.metric === 'purity') return { metric: r.metric, enabled: r.enabled, max_score: r.max_score };
  if (r.metric === 'speed') return { metric: r.metric, enabled: r.enabled, min_download_mbps: r.min_download_mbps };
  return { metric: r.metric, enabled: r.enabled };  // 新增指标待本卡片跟进前只启停
}

const form = reactive<RuleForm[]>(props.rules.map(fromSetting));
const baseline = ref(JSON.stringify(form.map(toPatchItem)));
const saving = ref(false);

const ordered = computed(() => [...form].sort((a, b) => a.priority - b.priority));

// 未编辑时跟随服务端值；编辑中不覆盖用户输入
watch(
  () => props.rules,
  (rules) => {
    if (dirty.value) return;
    form.splice(0, form.length, ...rules.map(fromSetting));
    baseline.value = JSON.stringify(form.map(toPatchItem));
  },
);

const dirty = computed(() => JSON.stringify(form.map(toPatchItem)) !== baseline.value);

function validate(): string | null {
  for (const r of form) {
    if (r.metric === 'latency' && !(r.max_latency > 0)) return '延时阈值必须为正数';
    if (r.metric === 'purity'
      && (!Number.isInteger(r.max_score) || r.max_score < 0 || r.max_score > 100)) {
      return '纯净度上限必须为 0-100 整数';
    }
    if (r.metric === 'speed' && !(r.min_download_mbps > 0)) return '测速下限必须为正数';
  }
  return null;
}

async function save() {
  const problem = validate();
  if (problem) {
    toast.error('参数错误', problem);
    return;
  }
  saving.value = true;
  try {
    const d = await api.settings({ enqueue_rules: form.map(toPatchItem) });
    toast.success('已更新', '入队筛选规则即刻生效');
    form.splice(0, form.length, ...d.settings.enqueue_rules.map(fromSetting));
    baseline.value = JSON.stringify(form.map(toPatchItem));
  } catch (e) {
    toast.error('保存失败', errText(e));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div data-testid="enqueue-card" class="card">
    <div class="card-header"><div class="card-title">入队筛选规则</div></div>
    <div class="card-body space-y-3 text-sm">
      <div class="text-xs text-[var(--color-t-muted)]">
        执行顺序（数字小先测，未通过即淘汰，后续指标不再测量）：
        <span v-for="(r, i) in ordered" :key="r.metric">
          <span v-if="i"> → </span>
          <span :class="r.enabled ? '' : 'opacity-50'">{{ r.label }}({{ r.priority }})</span>
        </span>
      </div>
      <div
        v-for="r in form"
        :key="r.metric"
        class="flex items-center justify-between gap-3"
      >
        <label class="flex items-center gap-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            class="accent-[var(--color-primary-600)]"
            :data-testid="`enq-toggle-${r.metric}`"
            v-model="r.enabled"
          >
          <span :class="r.enabled ? '' : 'text-[var(--color-t-muted)]'">{{ r.label }}</span>
        </label>
        <div v-if="r.metric === 'latency'" class="flex items-center gap-1">
          <input v-model.number="r.max_latency" data-testid="enq-max-latency" type="number" class="input w-20 text-right" min="0.05" step="0.05">
          <span class="text-xs text-[var(--color-t-muted)]">s 内</span>
        </div>
        <div v-else-if="r.metric === 'country'" class="flex items-center gap-1">
          <input v-model="r.allowText" data-testid="enq-allow" type="text" class="input w-48" placeholder="US, JP（空 = 不限制）">
        </div>
        <div v-else-if="r.metric === 'purity'" class="flex items-center gap-1">
          <input v-model.number="r.max_score" data-testid="enq-max-score" type="number" class="input w-20 text-right" min="0" max="100" step="1">
          <span class="text-xs text-[var(--color-t-muted)]">分以内</span>
        </div>
        <div v-else-if="r.metric === 'speed'" class="flex items-center gap-1">
          <input v-model.number="r.min_download_mbps" data-testid="enq-min-download" type="number" class="input w-20 text-right" min="0.5" step="0.5">
          <span class="text-xs text-[var(--color-t-muted)]">Mbps 起</span>
        </div>
      </div>
      <div class="text-xs text-[var(--color-t-muted)]">
        未测到值的指标按通过处理；被淘汰的候选移入 enqueue-rejected 组，不再参与填充。
      </div>
      <div class="flex justify-end pt-1">
        <button data-testid="enq-save" class="btn btn-primary btn-sm" :disabled="!dirty || saving" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
