import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

/**
 * 日志面板探针页构建（npm run build:probe）。
 *
 * 探针要灌几千条合成日志，真实 serve 的后端缓冲喂不到这个量级，因此单独构建一个静态探针页
 * 供 Playwright 用真实浏览器驱动；产物输出到 build-artifacts/logpanel-probe（已 gitignore），
 * 与面板产物目录互不干扰。
 */
export default defineConfig({
  root: fileURLToPath(new URL('./probe', import.meta.url)),
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: fileURLToPath(new URL('../build-artifacts/logpanel-probe', import.meta.url)),
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      input: { logpanel: fileURLToPath(new URL('./probe/logpanel.html', import.meta.url)) },
    },
  },
});