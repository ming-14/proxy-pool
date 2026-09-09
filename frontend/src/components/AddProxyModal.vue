<script setup lang="ts">
/** 添加代理模态框：多行/逗号/分号分隔输入，提交后由父组件调 API。 */
import { ref } from 'vue';

const props = defineProps<{ groupLabel: string }>();
const emit = defineEmits<{
  (e: 'submit', proxies: string[], done: () => void): void;
  (e: 'close'): void;
}>();

const text = ref('');
const submitting = ref(false);

function submit() {
  const proxies = text.value.split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean);
  if (!proxies.length) return;
  submitting.value = true;
  emit('submit', proxies, () => {
    submitting.value = false;
  });
}
</script>

<template>
  <div class="modal-overlay" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">添加代理到 {{ groupLabel }}</div>
        <button class="btn btn-ghost btn-sm" @click="emit('close')">&times;</button>
      </div>
      <div class="modal-body">
        <textarea
          v-model="text"
          class="input w-full"
          rows="8"
          placeholder="每行一个代理地址，如&#10;http://1.2.3.4:8080&#10;socks5://5.6.7.8:1080&#10;9.9.9.9:3128（裸地址默认 http）"
        />
      </div>
      <div class="modal-footer">
        <button class="btn btn-ghost btn-sm" @click="emit('close')">取消</button>
        <button class="btn btn-primary btn-sm" :disabled="submitting" @click="submit">
          {{ submitting ? '添加中...' : '添加' }}
        </button>
      </div>
    </div>
  </div>
</template>
