import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const backend = process.env.BACKEND_URL ?? 'http://host.docker.internal:8000'
const toBackend = { target: backend, changeOrigin: true }
const port = Number(process.env.FE_PORT ?? 5173)
const allowedHosts = process.env.ALLOWED_HOSTS?.split(',')
  .map((host) => host.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: true,
    port,
    strictPort: true,
    allowedHosts,
    // bind mounts don't always deliver inotify events into the container
    watch: { usePolling: true },
    proxy: {
      '/api': toBackend,
      '/health': toBackend,
      '/ready': toBackend,
      '/version': toBackend,
    },
  },
})
