import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import { router } from './router';
import { useStatusStore } from './stores/status';
import './styles/theme.css';
import './styles/components.css';

const app = createApp(App);
app.use(createPinia());
app.use(router);

// 轮询在 pinia 就绪后启动（store 内部 2s tick，首帧立即拉一次）
router.isReady().then(() => {
  useStatusStore().start();
});

app.mount('#app');
