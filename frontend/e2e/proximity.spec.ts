import { expect, test } from '@playwright/test'

// P6.5 Done-when, as DevTools → Sensors would do it: a location near the mock lot (51.5007,
// -0.1246, radius 500 m) shows the banner and one local notification while the app is open.
// The banner on both projects; the notification on Chromium only (as in alerts.spec, WebKit here
// is iPhone Safari outside the installed app, which has no notifications).
const FAR = { latitude: 51.5207, longitude: -0.1246, accuracy: 30 } // ≈ 2.2 km north
const NEAR = { latitude: 51.5043, longitude: -0.1246, accuracy: 30 } // ≈ 400 m north

test('entering the radius shows the banner and one local notification', async ({
  page,
  context,
  browserName,
}) => {
  await context.grantPermissions([
    'geolocation',
    ...(browserName === 'chromium' ? ['notifications'] : []),
  ])
  await context.setGeolocation(FAR)
  await page.addInitScript(() =>
    localStorage.setItem('parking.pushPrefs', JSON.stringify({ proximity: true })),
  )
  await page.goto('./')
  await page.evaluate(() => navigator.serviceWorker.ready)
  await expect(page.getByTestId('big-count')).toBeVisible()
  await expect(page.getByTestId('proximity-banner')).toHaveCount(0)

  await context.setGeolocation(NEAR)
  const banner = page.getByTestId('proximity-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText(
    /You're 400 m away · \d+ free \(Ground \d+ · Underground ≈\d+\)/,
  )

  if (browserName === 'chromium') {
    const shown = () =>
      page.evaluate(async () =>
        (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => [
          n.title,
          n.tag,
          n.body.startsWith("You're 400 m away"),
        ]),
      )
    await expect.poll(shown).toEqual([["You're near the parking", 'parking-status', true]])
  }

  // Out and back in: no second alert within 2 h, even after a reload.
  await banner.getByRole('button', { name: 'Hide this alert' }).click()
  await context.setGeolocation(FAR)
  await page.reload()
  await context.setGeolocation(NEAR)
  await page.waitForTimeout(1500)
  await expect(page.getByTestId('proximity-banner')).toHaveCount(0)
})
