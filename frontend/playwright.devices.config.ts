import { defineConfig, devices } from '@playwright/test'

// P6.8 device test matrix (notifications.md §6), live: the app built against a real `parking api`
// with VAPID keys, push delivered by the browser's real push service (FCM for Chromium). Run it
// with scripts/device-matrix.sh, which starts the API and the built app and sets the variables
// below. One project per row of the matrix; the phones are emulated (viewport, touch, user agent).
//
// Every project runs on Chromium, the only engine with Web Push in Playwright: the iPhone rows use
// the iPhone 13 profile (Safari's user agent, so the app takes its iPhone paths) on Chromium, and
// "installed" sets `navigator.standalone` the way a home-screen app does. Safari's own engine is
// covered by the mock suite on WebKit in CI (e2e/), and on a real iPhone in docs/go-live.md.
const chromiumPath = process.env.PW_CHROMIUM_PATH
const chromium = chromiumPath
  ? { launchOptions: { executablePath: chromiumPath } }
  : { channel: 'chromium' }
// Reminders are set in this time zone, and the API reads them in the subscription's tz.
const common = { timezoneId: 'Europe/Bucharest', locale: 'en-GB' }

export default defineConfig({
  testDir: './e2e-devices',
  // One lot and one API: the tests change its counts, so they run one after another.
  workers: 1,
  // The push service is on the internet: a push it drops or a slow answer from FCM on the dev
  // Pi's link fails a test that the next try passes. Retries show as "flaky" in the report.
  retries: 1,
  timeout: 240_000,
  reporter: [
    ['list'],
    ['json', { outputFile: process.env.DM_REPORT ?? 'test-results/devices.json' }],
  ],
  use: { baseURL: process.env.DM_APP_URL ?? 'http://localhost:4175/', ...common },
  projects: [
    { name: 'Android Chrome', use: { ...devices['Pixel 7'], ...chromium } },
    {
      name: 'iPhone installed',
      use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium', ...chromium },
    },
    {
      name: 'iPhone Safari',
      use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium', ...chromium },
    },
    { name: 'Desktop Chrome', use: { ...devices['Desktop Chrome'], ...chromium } },
  ],
})
