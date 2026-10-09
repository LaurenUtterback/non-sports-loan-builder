import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// During development the Vue dev server runs on :5174 and proxies /api calls
// to the FastAPI backend on :8001, so there are no CORS surprises locally.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
