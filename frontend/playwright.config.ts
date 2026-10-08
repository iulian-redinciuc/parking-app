import { defineConfig, devices } from '@playwright/test'

// E2E tests run against the production build with mock data (VITE_API_BASE=mock).
export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://localhost:4173/parking-app/' },
  projects: [
    { name: 'iPhone 13', use: { ...devices['iPhone 13'] } },
    { name: 'Pixel 7', use: { ...devices['Pixel 7'] } },
  ],
  webServer: {
    command: 'npm run build -- --mode development && npm run preview -- --port 4173 --strictPort',
    url: 'http://localhost:4173/parking-app/',
    reuseExistingServer: !process.env.CI,
  },
})
