<script setup lang="ts">
/**
 * 应用壳：侧边栏 + 顶栏（暂停/连接态/主题）+ 路由视图 + Toast + 移动端遮罩。
 * 结构与旧 index.html 逐块对应，类名与度量保持一致。
 */
import { computed, ref } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import Icon from '@/components/ui/Icon.vue';
import ToastHost from '@/components/ui/ToastHost.vue';
import type { IconName } from '@/components/ui/icons';
import { useLogPanel } from '@/composables/useLogPanel';
import { useStatusStore } from '@/stores/status';
import { useThemeStore } from '@/stores/theme';
import { LogPanel } from '@/vendor/logpanel';

const route = useRoute();
const statusStore = useStatusStore();
const themeStore = useThemeStore();
// 控制台日志面板（侧边栏底部按钮启停；面板隐藏时不发 /api/logs 请求）
const {
  visible: logPanelVisible,
  source: logPanelSource,
  rules: logPanelRules,
  toggle: toggleLogPanel,
} = useLogPanel();

const pageTitle = computed(() => String(route.meta.title ?? '仪表盘'));
const sidebarOpen = ref(false);

/** 导航项（图标见 components/ui/icons.ts；name 是路由名，icon 是图标名） */
const NAV: { name: string; label: string; icon: IconName }[] = [
  { name: 'dashboard', label: '仪表盘', icon: 'dashboard' },
  { name: 'groups', label: '代理组', icon: 'groups' },
  { name: 'standby', label: '代理服务队列', icon: 'activity' },
  { name: 'sources', label: '代理源', icon: 'sources' },
  { name: 'settings', label: '设置', icon: 'settings' },
];
</script>

<template>
  <!-- 侧边栏 -->
  <aside class="sidebar flex flex-col" :class="{ open: sidebarOpen }">
    <div class="h-16 flex items-center px-5 border-b border-[var(--color-line-light)]">
      <Icon name="globe" class="w-7 h-7 text-[var(--color-primary-600)] flex-shrink-0" />
      <span class="ml-3 font-bold text-lg tracking-tight sidebar-text">Proxy Pool</span>
    </div>
    <nav class="flex-1 py-4 px-3 space-y-1 overflow-y-auto">
      <RouterLink
        v-for="item in NAV"
        :key="item.name"
        :to="{ name: item.name }"
        class="nav-item"
        active-class="active"
        :data-testid="`nav-${item.name}`"
        @click="sidebarOpen = false"
      >
        <Icon :name="item.icon" class="w-5 h-5" />
        <span class="sidebar-text">{{ item.label }}</span>
      </RouterLink>
    </nav>
    <div class="p-4 border-t border-[var(--color-line-light)]">
      <button
        data-testid="btn-logpanel"
        class="btn btn-ghost btn-sm w-full justify-start gap-2"
        :class="{ 'btn-primary': logPanelVisible }"
        title="控制台日志"
        @click="toggleLogPanel"
      >
        <Icon name="terminal" class="w-4 h-4 flex-shrink-0" />
        <span class="sidebar-text">控制台日志</span>
      </button>
    </div>
  </aside>

  <!-- 主区域 -->
  <div class="flex-1 flex flex-col min-w-0">
    <header class="h-16 border-b border-[var(--color-line)] flex items-center justify-between px-5 flex-shrink-0 bg-[var(--color-card)]">
      <div class="flex items-center gap-3">
        <button class="lg:hidden p-2 rounded-lg hover:bg-[var(--color-hover)] text-[var(--color-t-secondary)]" @click="sidebarOpen = !sidebarOpen">
          <Icon name="menu" class="w-5 h-5" />
        </button>
        <h1 data-testid="page-title" class="text-lg font-semibold text-[var(--color-t-primary)]">{{ pageTitle }}</h1>
      </div>
      <div class="flex items-center gap-3">
        <button data-testid="btn-pause" class="btn btn-sm gap-1.5" :class="statusStore.paused ? 'btn-primary' : 'btn-ghost'" title="暂停/恢复轮询" @click="statusStore.togglePause()">
          <Icon :name="statusStore.paused ? 'play' : 'pause'" class="w-4 h-4" />
          <span>{{ statusStore.paused ? '继续' : '暂停' }}</span>
        </button>
        <div class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-[var(--color-hover)] text-xs font-medium">
          <span data-testid="conn-dot" class="conn-dot" :class="statusStore.connected ? 'online' : 'offline'" />
          <span data-testid="conn-text" :class="statusStore.connected ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'">
            {{ statusStore.connected ? '已连接' : '已断开' }}
          </span>
        </div>
        <button data-testid="btn-theme" class="btn btn-ghost btn-sm" title="切换主题" @click="themeStore.toggle()">
          <Icon :name="themeStore.theme !== 'dark' ? 'sun' : 'moon'" class="w-5 h-5" />
        </button>
      </div>
    </header>

    <main id="main-content" class="main-content flex-1 overflow-y-auto p-5 lg:p-6">
      <RouterView v-slot="{ Component }">
        <div :key="route.name" class="page-section">
          <component :is="Component" />
        </div>
      </RouterView>
    </main>

    <footer class="border-t border-[var(--color-line)] px-5 py-2.5 text-xs text-[var(--color-t-muted)] flex justify-between flex-shrink-0 bg-[var(--color-card)]">
      <span>made by Rikka</span>
    </footer>
  </div>

  <!-- 移动端遮罩 -->
  <div v-show="sidebarOpen" class="fixed inset-0 bg-black/30 z-30 lg:hidden" @click="sidebarOpen = false" />

  <ToastHost />

  <LogPanel
    v-model:visible="logPanelVisible"
    :source="logPanelSource"
    :rules="logPanelRules"
    :theme="themeStore.theme"
    title="控制台日志"
    storage-key-prefix="proxy-pool-logpanel_"
  />
</template>
