import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// `/parking-app/` for the GitHub Pages preview; production usually sets VITE_BASE=/.
// The PWA plugin is added in P3.7.
export default defineConfig({
  base: process.env.VITE_BASE ?? '/parking-app/',
  plugins: [react(), tailwindcss()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
  },
})
