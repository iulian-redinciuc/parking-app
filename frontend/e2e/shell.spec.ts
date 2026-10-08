import { expect, test } from '@playwright/test'

// P3.1: navigation works at 320 px wide with no horizontal scroll, in both themes.
for (const colorScheme of ['light', 'dark'] as const) {
  test.describe(`app shell, ${colorScheme} theme, 320 px`, () => {
    test.use({ colorScheme, viewport: { width: 320, height: 640 } })

    test('bottom nav switches screens without horizontal scroll', async ({ page }) => {
      await page.goto('./')
      const noHorizontalScroll = () =>
        page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)

      await expect(page.getByRole('heading', { name: 'Live' })).toBeVisible()
      expect(await noHorizontalScroll()).toBe(true)

      for (const [tab, hash] of [
        ['Alerts', '#/alerts'],
        ['Stats', '#/stats'],
        ['Live', '#/'],
      ]) {
        const link = page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: tab })
        const box = await link.boundingBox()
        expect(box!.height).toBeGreaterThanOrEqual(44)
        expect(box!.width).toBeGreaterThanOrEqual(44)
        await link.click()
        await expect(page.getByRole('heading', { name: tab })).toBeVisible()
        await expect(link).toHaveAttribute('aria-current', 'page')
        expect(new URL(page.url()).hash).toBe(hash)
        expect(await noHorizontalScroll()).toBe(true)
      }
    })

    test('uses the theme tokens', async ({ page }) => {
      await page.goto('./')
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
      expect(bg).toBe(colorScheme === 'dark' ? 'rgb(13, 17, 23)' : 'rgb(255, 255, 255)')
    })
  })
}
