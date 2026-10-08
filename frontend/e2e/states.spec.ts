import { expect, test } from '@playwright/test'

// P3.5: every edge state of the Live screen can be forced in mock mode with ?mock=<scenario>
// (frontend.md §2.1) and looks right at 320 px in both themes.
const SCENARIOS = [
  { mock: 'loading', banner: null, numbers: 'skeleton' },
  { mock: 'offline', banner: "You're offline", numbers: 'all greyed' },
  { mock: 'unreachable', banner: "Can't reach the parking server", numbers: 'all greyed' },
  { mock: 'unavailable', banner: 'Waiting for the first camera reading', numbers: 'hidden' },
  { mock: 'stale', banner: /^Camera data is 5 min old/, numbers: 'stale zone greyed' },
  { mock: 'estimated', banner: null, numbers: 'estimated' },
] as const

for (const colorScheme of ['light', 'dark'] as const) {
  test.describe(`edge states, ${colorScheme} theme, 320 px`, () => {
    test.use({ colorScheme, viewport: { width: 320, height: 640 } })

    for (const { mock, banner, numbers } of SCENARIOS) {
      test(`?mock=${mock}`, async ({ page }, info) => {
        await page.goto(`./?mock=${mock}#/`)
        const bannerEl = page.getByTestId('status-banner')
        const count = page.getByTestId('big-count')
        const ground = page.getByTestId('zone-ground')
        const underground = page.getByTestId('zone-underground')

        if (banner === null) await expect(bannerEl).toHaveCount(0)
        else await expect(bannerEl).toContainText(banner)

        switch (numbers) {
          case 'skeleton':
            await expect(page.getByTestId('live-skeleton')).toBeVisible()
            await expect(count).toHaveCount(0)
            break
          case 'hidden':
            await expect(count).toHaveCount(0)
            await expect(page.getByTestId('live-skeleton')).toHaveCount(0)
            break
          case 'all greyed':
            await expect(count).toBeVisible()
            await expect(count.locator('xpath=ancestor::section')).toHaveClass(/dimmed/)
            await expect(ground).toHaveClass(/dimmed/)
            await expect(underground).toHaveClass(/dimmed/)
            break
          case 'stale zone greyed':
            await expect(count.locator('xpath=ancestor::section')).not.toHaveClass(/dimmed/)
            await expect(ground).toHaveClass(/dimmed/)
            await expect(ground).toContainText('No fresh camera data')
            await expect(underground).not.toHaveClass(/dimmed/)
            break
          case 'estimated':
            await expect(underground).toContainText(/≈ \d+ \/ 60/)
            await expect(underground).toContainText('Estimated from entry/exit counts')
            await expect(count).toHaveText(/^≈ \d+$/)
            break
        }
        if (numbers === 'all greyed' || numbers === 'stale zone greyed') {
          // greyed = the muted token, not the normal text colour
          const colours = await ground.evaluate((el) => {
            const probe = el.querySelector('h2')!
            const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted')
            const tmp = document.createElement('span')
            tmp.style.color = muted
            document.body.append(tmp)
            const expected = getComputedStyle(tmp).color
            tmp.remove()
            return [getComputedStyle(probe).color, expected]
          })
          expect(colours[0]).toBe(colours[1])
        }
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        ).toBe(true)
        await page.screenshot({ path: info.outputPath(`${mock}-${colorScheme}.png`) })
      })
    }

    test('a real loss of network shows the offline banner', async ({ page, context }) => {
      await page.goto('./?mock=estimated#/')
      await expect(page.getByTestId('big-count')).toBeVisible()
      await context.setOffline(true)
      await expect(page.getByTestId('status-banner')).toContainText("You're offline")
      await context.setOffline(false)
      await expect(page.getByTestId('status-banner')).toHaveCount(0)
    })
  })
}
