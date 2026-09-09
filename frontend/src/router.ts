/**
 * 路由：hash 模式保持旧版 URL（#dashboard / #groups / #standby / #sources）不变。
 * historyEnabled 副作用：仅仪表盘/队列页需要历史增量拉取（与旧版页面判定一致）。
 */
import { createRouter, createWebHashHistory } from 'vue-router';
import { useStatusStore } from '@/stores/status';

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: () => import('@/pages/DashboardPage.vue'), meta: { title: '仪表盘' } },
  { path: '/groups', name: 'groups', component: () => import('@/pages/GroupsPage.vue'), meta: { title: '代理组' } },
  { path: '/standby', name: 'standby', component: () => import('@/pages/StandbyPage.vue'), meta: { title: '代理服务队列' } },
  { path: '/sources', name: 'sources', component: () => import('@/pages/SourcesPage.vue'), meta: { title: '代理源' } },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

router.afterEach((to) => {
  const page = String(to.name ?? '');
  useStatusStore().historyEnabled = page === 'dashboard' || page === 'standby';
});
