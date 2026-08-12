import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true } },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('element-plus')) return 'element-plus'
          if (id.includes('node_modules/vue') || id.includes('node_modules/pinia')) return 'vue-vendor'
        },
      },
    },
  },
  test: { environment: 'jsdom', globals: true, exclude: ['tests/e2e/**', 'node_modules/**'] },
})
