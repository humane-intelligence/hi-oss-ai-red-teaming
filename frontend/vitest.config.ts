import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  // openapi-fetch needs an absolute baseUrl so fetch() can construct a valid URL in Node
  define: {
    'import.meta.env.VITE_API_BASE_URL': JSON.stringify('http://localhost'),
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // The `userEvent` heavy specs exceed the 5s default only under a full parallel run.
    testTimeout: 20_000,
    environmentOptions: {
      jsdom: { url: 'http://localhost' },
    },
  },
})
