import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { searchForWorkspaceRoot } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'
import { defineConfig } from 'vitest/config'

// `/parking-app/` for the GitHub Pages preview; production usually sets VITE_BASE=/.
const base = process.env.VITE_BASE ?? '/parking-app/'

// The admin's slot/line editor (P7.3) is the standalone tool's core, imported as `@slot-editor`
// (types in src/types/slot-editor.d.ts), so both stay one implementation.
const slotEditor = fileURLToPath(new URL('../tools/slot-editor', import.meta.url))

export default defineConfig({
  base,
  resolve: { alias: { '@slot-editor': `${slotEditor}/editor.js` } },
  server: { fs: { allow: [searchForWorkspaceRoot(process.cwd()), slotEditor] } },
  plugins: [
    react(),
    tailwindcss(),
    // PWA (frontend.md §5): our own service worker in src/sw.ts, the plugin injects the precache
    // list and writes the manifest. Registered by the injected registerSW.js; not active in `vite dev`.
    VitePWA({
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      registerType: 'autoUpdate',
      injectManifest: { globPatterns: ['**/*.{js,css,html,svg,png,webmanifest}'] },
      manifest: {
        name: 'Parking',
        short_name: 'Parking',
        description: 'Live free parking spaces',
        start_url: `${base}#/`,
        scope: base,
        display: 'standalone',
        background_color: '#0d1117',
        theme_color: '#0d1117',
        icons: [
          { src: 'icons/192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'icons/maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
    }),
  ],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
  },
})
