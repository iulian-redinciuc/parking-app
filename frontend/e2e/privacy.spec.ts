import { expect, test } from '@playwright/test'

// P8.9 (frontend.md §2.5): the Privacy screen is reached from the footer and from the Alerts
// screen, and reads without horizontal scroll at 320 px in both themes.
for (const colorScheme of ['light', 'dark'] as const) {
  test.describe(`privacy screen, ${colorScheme} theme, 320 px`, () => {
    test.use({ colorScheme, viewport: { width: 320, height: 640 } })

    test('footer link opens it; the bottom nav never covers the link', async ({ page }) => {
      await page.goto('./')
      await expect(page.getByRole('heading', { name: 'Live' })).toBeVisible()
      const link = page.getByRole('contentinfo').getByRole('link', { name: 'Privacy' })
      // (scrollIntoView counts a link under the fixed nav as visible: go to the end instead)
      const toEnd = () => page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
      await toEnd()
      const box = (await link.boundingBox())!
      expect(box.height).toBeGreaterThanOrEqual(44)
      const nav = (await page.getByRole('navigation', { name: 'Main' }).boundingBox())!
      expect(box.y + box.height).toBeLessThanOrEqual(nav.y)
      await link.click()

      expect(new URL(page.url()).hash).toBe('#/privacy')
      await expect(page.getByRole('heading', { name: 'Privacy', level: 1 })).toBeVisible()
      await expect(page.getByText('No video is recorded')).toBeVisible()
      await expect(page.getByText('never sent to the server and never stored')).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Who is responsible' })).toBeVisible()
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      ).toBe(true)
      // the last line can be scrolled clear of the bottom nav
      const last = page.getByText('complain to your data protection authority')
      await toEnd()
      const lastBox = (await last.boundingBox())!
      expect(lastBox.y + lastBox.height).toBeLessThanOrEqual(nav.y)
    })

    test('the Alerts screen links to it', async ({ page }) => {
      await page.goto('./#/alerts')
      await page.getByRole('link', { name: 'How your data is handled' }).click()
      await expect(page.getByRole('heading', { name: 'Privacy', level: 1 })).toBeVisible()
    })
  })
}
