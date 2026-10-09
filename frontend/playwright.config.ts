import { defineConfig, devices } from '@playwright/test'

// E2E tests run against the production build with mock data (VITE_API_BASE=mock).
// PW_CHROMIUM_PATH runs the Chromium projects on a system browser (e.g. /usr/bin/chromium on the
// dev Pi, where Playwright's own browsers aren't installed); a system Chromium runs new headless.
const chromiumPath = process.env.PW_CHROMIUM_PATH

export default defineConfig({
  testDir: './e2e',
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: { baseURL: 'http://localhost:4173/parking-app/' },
  projects: [
    { name: 'iPhone 13', use: { ...devices['iPhone 13'] } },
    {
      name: 'Pixel 7',
      use: {
        ...devices['Pixel 7'],
        // Full Chromium in new headless mode, not the headless shell, which shows no notifications.
        ...(chromiumPath
          ? { launchOptions: { executablePath: chromiumPath } }
          : { channel: 'chromium' }),
      },
    },
  ],
  webServer: {
    command: 'npm run build -- --mode development && npm run preview -- --port 4173 --strictPort',
    url: 'http://localhost:4173/parking-app/',
    reuseExistingServer: !process.env.CI,
  },
})
