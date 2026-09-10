import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  // Relative, so a single build works everywhere it is served from: the
  // desktop shell serves it at the root, the web serves it under /app.
  // An absolute base would hard-code one and break the other, and two
  // builds would mean two things to keep in step.
  base: './',
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
