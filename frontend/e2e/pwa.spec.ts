import { expect, test } from '@playwright/test'

// P3.7 Done when: the manifest is valid (what DevTools → Application → Manifest checks), the
// service worker is active, and the app opens with no network showing the shell + offline banner.
// Chromium only: CDP and beforeinstallprompt are Chromium features.
test.describe('PWA', () => {
  test.skip(({ browserName }) => browserName !== 'chromium', 'Chromium DevTools checks')
  test.use({ viewport: { width: 320, height: 640 } })

  test('valid, installable manifest', async ({ page }) => {
    await page.goto('./#/')
    const cdp = await page.context().newCDPSession(page)
    const manifest = await cdp.send('Page.getAppManifest')
    expect(manifest.url).toMatch(/\/parking-app\/manifest\.webmanifest$/)
    expect(manifest.errors).toEqual([])
    const data = JSON.parse(manifest.data)
    expect(data).toMatchObject({
      name: 'Parking',
      display: 'standalone',
      start_url: '/parking-app/#/',
      scope: '/parking-app/',
    })
    for (const icon of data.icons) {
      const res = await page.request.get(new URL(icon.src, manifest.url).href)
      expect(res.status(), icon.src).toBe(200)
    }
    // Needs the active service worker, so wait for it first. Playwright's contexts are incognito,
    // which Chrome reports as the only reason not to offer installing.
    await page.evaluate(() => navigator.serviceWorker.ready)
    await expect
      .poll(async () =>
        (await cdp.send('Page.getInstallabilityErrors')).installabilityErrors
          .map((e) => e.errorId)
          .filter((id) => id !== 'in-incognito'),
      )
      .toEqual([])
  })

  test('active service worker; opens offline with the shell and offline banner', async ({
    page,
    context,
  }) => {
    await page.goto('./#/')
    await expect
      .poll(() => page.evaluate(async () => (await navigator.serviceWorker.ready).active?.state))
      .toBe('activated')
    await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true)

    // Only the app shell is cached: no API responses.
    const cached = await page.evaluate(async () => {
      const urls: string[] = []
      for (const name of await caches.keys())
        for (const req of await (await caches.open(name)).keys()) urls.push(req.url)
      return urls
    })
    expect(cached.some((u) => u.includes('index.html'))).toBe(true)
    expect(cached.filter((u) => /\/api\//.test(new URL(u).pathname))).toEqual([])

    await context.setOffline(true)
    await page.reload()
    await expect(page.getByRole('banner')).toContainText('Parking')
    await expect(page.getByRole('navigation')).toBeVisible()
    await expect(page.getByTestId('status-banner')).toContainText("You're offline")

    // A fresh tab, offline from the start, opens too.
    const other = await context.newPage()
    await other.goto('./#/stats')
    await expect(other.getByRole('heading', { name: 'Stats' })).toBeVisible()
    await context.setOffline(false)
  })

  test('the install button uses the saved beforeinstallprompt', async ({ page }) => {
    await page.goto('./#/')
    await page.evaluate(() => {
      const e = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), {
        prompt: () => {
          ;(window as unknown as { prompted: boolean }).prompted = true
          return Promise.resolve()
        },
        userChoice: Promise.resolve({ outcome: 'dismissed' }),
      })
      window.dispatchEvent(e)
    })
    const button = page.getByRole('button', { name: 'Install app' })
    await expect(button).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
    await button.click()
    expect(await page.evaluate(() => (window as unknown as { prompted?: boolean }).prompted)).toBe(
      true,
    )
    await expect(button).toHaveCount(0)
  })
})
