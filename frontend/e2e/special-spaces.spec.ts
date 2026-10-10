import { expect, test } from '@playwright/test'

// P9.2: the special-space chips on the ground card and the Alerts setting, in mock mode
// (frontend.md §2.1, §2.2). The mock lot's ground zone has 2 accessible spaces and 2 EV chargers.
for (const colorScheme of ['light', 'dark'] as const) {
  test.describe(`special spaces, ${colorScheme} theme, 320 px`, () => {
    test.use({ colorScheme, viewport: { width: 320, height: 640 } })

    test('the ground card has a chip per kind of special space', async ({ page }, info) => {
      await page.goto('./')
      const card = page.getByTestId('zone-ground')
      const chips = card.getByRole('group', { name: 'Special spaces' })
      await expect(chips.getByTestId('special-accessible')).toHaveText(/^Accessible [0-2] \/ 2/)
      await expect(chips.getByTestId('special-ev')).toHaveText(/^EV charging [0-2] \/ 2/)
      await expect(chips.getByText(/^Accessible: [0-2] of 2 free$/)).toBeAttached()
      // a zone counted from entries and exits has no spaces to tell apart
      await expect(page.getByTestId('zone-underground').getByRole('group')).toHaveCount(0)
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      ).toBe(true)
      await page.screenshot({ path: info.outputPath(`special-${colorScheme}.png`), fullPage: true })
    })
  })
}
