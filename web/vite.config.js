import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built SPA is served by FastAPI from web/dist, so the dev server proxies the API
// to the backend and the production bundle uses same-origin relative URLs.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
