import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const target = 'http://127.0.0.1:8400'
export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 900 },
  server: { proxy: Object.fromEntries(['/api', '/live', '/media', '/poster', '/logo', '/health'].map((p) => [p, target])) },
})
