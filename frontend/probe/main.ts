/** 探针入口：挂载 ProbeApp（产物供 tests/e2e/web/test_logpanel_virtual.py 驱动） */
import { createApp } from 'vue';
import ProbeApp from './ProbeApp.vue';

createApp(ProbeApp).mount('#app');