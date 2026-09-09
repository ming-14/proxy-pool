<script setup lang="ts">
/**
 * 应用壳：侧边栏 + 顶栏（暂停/连接态/主题）+ 路由视图 + Toast + 移动端遮罩。
 * 结构与旧 index.html 逐块对应，类名与度量保持一致。
 */
import { computed, ref } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import ToastHost from '@/components/ui/ToastHost.vue';
import { useStatusStore } from '@/stores/status';
import { useThemeStore } from '@/stores/theme';

const route = useRoute();
const statusStore = useStatusStore();
const themeStore = useThemeStore();

const pageTitle = computed(() => String(route.meta.title ?? '仪表盘'));
const sidebarOpen = ref(false);

// 导航项（图标路径与旧版 SVG 一致）
const NAV = [
  {
    name: 'dashboard', label: '仪表盘',
    paths: ['<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>'],
  },
  {
    name: 'groups', label: '代理组',
    paths: [
      '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    ],
  },
  {
    name: 'standby', label: '代理服务队列',
    paths: ['<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>'],
  },
  {
    name: 'sources', label: '代理源',
    paths: [
      '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    ],
  },
];
</script>

<template>
  <!-- 侧边栏 -->
  <aside class="sidebar flex flex-col" :class="{ open: sidebarOpen }">
    <div class="h-16 flex items-center px-5 border-b border-[var(--color-line-light)]">
      <svg class="w-7 h-7 text-[var(--color-primary-600)] flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M2 12h20" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
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
        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" v-html="item.paths.join('')" />
        <span class="sidebar-text">{{ item.label }}</span>
      </RouterLink>
    </nav>
    <div class="p-4 border-t border-[var(--color-line-light)] sidebar-text">
      <div class="text-xs text-[var(--color-t-muted)]">v7</div>
    </div>
  </aside>

  <!-- 主区域 -->
  <div class="flex-1 flex flex-col min-w-0">
    <header class="h-16 border-b border-[var(--color-line)] flex items-center justify-between px-5 flex-shrink-0 bg-[var(--color-card)]">
      <div class="flex items-center gap-3">
        <button class="lg:hidden p-2 rounded-lg hover:bg-[var(--color-hover)] text-[var(--color-t-secondary)]" @click="sidebarOpen = !sidebarOpen">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>
        <h1 data-testid="page-title" class="text-lg font-semibold text-[var(--color-t-primary)]">{{ pageTitle }}</h1>
      </div>
      <div class="flex items-center gap-3">
        <button data-testid="btn-pause" class="btn btn-sm gap-1.5" :class="statusStore.paused ? 'btn-primary' : 'btn-ghost'" title="暂停/恢复轮询" @click="statusStore.togglePause()">
          <svg v-if="!statusStore.paused" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" />
          </svg>
          <svg v-else class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="5 3 19 12 5 21 5 3" />
          </svg>
          <span>{{ statusStore.paused ? '继续' : '暂停' }}</span>
        </button>
        <div class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-[var(--color-hover)] text-xs font-medium">
          <span data-testid="conn-dot" class="conn-dot" :class="statusStore.connected ? 'online' : 'offline'" />
          <span data-testid="conn-text" :class="statusStore.connected ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'">
            {{ statusStore.connected ? '已连接' : '已断开' }}
          </span>
        </div>
        <button data-testid="btn-theme" class="btn btn-ghost btn-sm" title="切换主题" @click="themeStore.toggle()">
          <svg v-if="themeStore.theme !== 'dark'" class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="5" /><line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" /><line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" /><line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" /><line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
          </svg>
          <svg v-else class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
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
</template>
