import { expect, test } from '@playwright/test'

// P7.7: #/stats on the mock build (8 weeks of synthetic history): the forecast card, the today
// chart drawn by the lazily loaded Recharts with its legend, the heatmap, the screen-reader
// tables, and switching zones; in light and dark.
for (const colorScheme of ['light', 'dark'] as const) {
  test(`stats screen (${colorScheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme })
    await page.goto('./#/stats')
    await expect(page.getByRole('heading', { name: 'Stats' })).toBeVisible()
    await expect(page.getByTestId('forecast')).toHaveText(/^Usually ~\d+ free at \d/)
    const chart = page.getByTestId('today-chart')
    await expect(chart.locator('svg.recharts-surface')).toBeVisible()
    await expect(chart.locator('.recharts-line-curve').first()).toBeVisible()
    await expect(chart.getByText('Time of day')).toBeVisible()
    await expect(chart.getByText('Free spaces')).toBeVisible()
    await expect(page.getByTestId('heatmap').locator('[data-free]')).toHaveCount(7 * 24)
    await expect(page.getByRole('table', { name: 'Today' })).toBeAttached()
    await expect(
      page.getByRole('table', { name: /Average free spaces by weekday/ }).getByRole('row'),
    ).toHaveCount(8)

    await page.getByRole('button', { name: 'Underground' }).click()
    await expect(page.getByRole('button', { name: 'Underground' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await expect(page.getByTestId('forecast')).toHaveText(/^Usually ~\d+ free at /)
    await page.screenshot({ path: `test-results/stats-${colorScheme}.png`, fullPage: true })
  })
}
