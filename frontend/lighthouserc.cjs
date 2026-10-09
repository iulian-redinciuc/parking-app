// Lighthouse CI (P3.10, docs/design/testing.md §1): the mock-mode preview build, mobile preset
// (Lighthouse's default: Moto G Power emulation + simulated slow 4G). Lighthouse 12 dropped the PWA
// category, so installability is checked by Playwright (e2e/pwa.spec.ts) instead.
// Run: npm run build -- --mode development && npm run lhci  (CHROME_PATH for a system Chromium)
module.exports = {
  ci: {
    collect: {
      startServerCommand: 'npm run preview -- --port 4173 --strictPort',
      startServerReadyPattern: 'localhost:4173',
      url: ['http://localhost:4173/parking-app/'],
      numberOfRuns: 3,
      settings: { chromeFlags: '--no-sandbox --headless=new' },
    },
    assert: {
      assertions: {
        'categories:performance': ['error', { minScore: 0.9 }],
        'categories:accessibility': ['error', { minScore: 0.9 }],
      },
    },
    upload: { target: 'filesystem', outputDir: '.lighthouseci/reports' },
  },
}
