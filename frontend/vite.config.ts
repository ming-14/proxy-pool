import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import tailwindcss from '@tailwindcss/vite';

// 构建产物直接输出到 aiohttp 静态目录（src/framework/web/static），挂载点不变。
// base '/'：产物内绝对路径引用，与后端 /assets 路由约定一致。
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../src/framework/web/static',
    emptyOutDir: true,
    target: 'es2022',
  },
  server: {
    // 开发模式：vite dev server 代理到真实面板后端（默认 8083）
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8083',
    },
  },
});
