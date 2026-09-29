import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发：dev server 在根路径，/api 代理到管家 5199
// 生产：构建产物由 FastAPI 挂在 /ui 下，故 base 为 /ui/
export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  base: mode === 'production' ? '/ui/' : '/',
  build: {
    outDir: fileURLToPath(new URL('../tts_hub/static', import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500,
  },
  server: {
    port: 5180,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5199',
        changeOrigin: true,
      },
    },
  },
}))
