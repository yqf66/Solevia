import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': env.VITE_BACKEND_URL || 'http://127.0.0.1:8000',
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: './src/test/setup.ts',
    },
  }
})
