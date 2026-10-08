import { expect, test } from '@playwright/test'

// P3.4: the Live screen in mock mode looks like the wireframe (frontend.md §2.1).
for (const colorScheme of ['light', 'dark'] as const) {
  test.describe(`live screen, ${colorScheme} theme, 320 px`, () => {
    test.use({ colorScheme, viewport: { width: 320, height: 640 } })

    test('shows the total, the zone cards and the update time', async ({ page }, info) => {
      await page.goto('./')
      const count = page.getByTestId('big-count')
      await expect(count).toHaveText(/^(≈ )?\d+$/)
      const fontSize = await count.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
      expect(fontSize).toBeGreaterThanOrEqual(72)
      await expect(page.getByText(/^free spaces?$/)).toBeVisible()

      const zones = page.getByRole('list', { name: 'Zones' }).getByRole('listitem')
      await expect(zones).toHaveCount(2)
      for (const [i, name, capacity] of [
        [0, 'Ground', 40],
        [1, 'Underground', 60],
      ] as const) {
        const card = zones.nth(i)
        await expect(card.getByRole('heading', { name })).toBeVisible()
        await expect(card).toContainText(new RegExp(`\\d+ / ${capacity}`))
        await expect(card).toContainText(/Plenty of space|Filling up|Almost full|Full/)
        await expect(card).toContainText(/Trend: (getting fuller|emptying|steady)/)
      }
      await expect(page.getByText(/^Updated /)).toBeVisible()
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      ).toBe(true)
      await page.screenshot({ path: info.outputPath(`live-${colorScheme}.png`) })
    })
  })
}
