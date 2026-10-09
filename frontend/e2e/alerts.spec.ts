import { expect, test } from '@playwright/test'

// P6.4: the Alerts screen on the mock build. Chromium (Pixel 7): no prompt until the tap, enable →
// settings → test → the notification is shown; P6.6: the I'm on my way chips. WebKit runs as
// iPhone Safari outside the installed app, so it gets the install hint instead of the button.
test.describe('Alerts', () => {
  test('iPhone Safari, not installed: the install hint instead of the button', async ({
    page,
    browserName,
  }) => {
    test.skip(browserName !== 'webkit', 'iPhone project only')
    await page.goto('./#/alerts')
    await expect(page.getByTestId('install-hint')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Enable notifications' })).toHaveCount(0)
  })

  test.describe('Chromium', () => {
    test.skip(({ browserName }) => browserName !== 'chromium', 'Chromium shows notifications')

    test('no prompt before the tap; enable → test → a notification arrives', async ({
      page,
      context,
    }) => {
      await page.goto('./#/alerts')
      await page.evaluate(() => navigator.serviceWorker.ready)
      const enable = page.getByRole('button', { name: 'Enable notifications' })
      await expect(enable).toBeVisible()
      expect(await page.evaluate(() => Notification.permission)).toBe('default')

      await context.grantPermissions(['notifications'])
      await enable.click()
      await expect(page.getByText('Notifications are on for this device.')).toBeVisible()

      // A setting is saved after the debounce and survives a reload.
      await page.getByRole('switch', { name: /Warn when almost full/ }).check()
      await expect(page.getByText('Saved')).toBeVisible()
      await page.reload()
      await expect(page.getByRole('switch', { name: /Warn when almost full/ })).toBeChecked()

      await page.getByRole('button', { name: 'Send test notification' }).click()
      await expect(page.getByText(/It should arrive in a few seconds/)).toBeVisible()
      await expect
        .poll(() =>
          page.evaluate(async () =>
            (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => [
              n.title,
              n.tag,
              /^\d+ free · /.test(n.body),
            ]),
          ),
        )
        .toEqual([['Parking test', 'parking-status', true]])

      await page.getByRole('button', { name: 'Turn off notifications' }).click()
      await expect(page.getByRole('button', { name: 'Enable notifications' })).toBeVisible()
    })

    test("I'm on my way: chip → first update + countdown → stop", async ({ page, context }) => {
      await page.goto('./#/alerts')
      await page.evaluate(() => navigator.serviceWorker.ready)
      await context.grantPermissions(['notifications'])
      await page.getByRole('button', { name: 'Enable notifications' }).click()
      await page.getByRole('button', { name: '15 min' }).click()
      await expect(page.getByRole('timer')).toHaveText(/^1[45]:\d\d left$/)
      await expect(page.getByText(/^Updates until /)).toBeVisible()
      await expect
        .poll(() =>
          page.evaluate(async () =>
            (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => [
              /^Parking: \d+ free$/.test(n.title),
              n.tag,
            ]),
          ),
        )
        .toEqual([[true, 'parking-status']])
      // the window survives a reload
      await page.reload()
      await expect(page.getByRole('timer')).toBeVisible()
      await page.getByRole('button', { name: 'Stop updates' }).click()
      await expect(page.getByRole('button', { name: '30 min' })).toBeVisible()
      await expect(page.getByRole('timer')).toHaveCount(0)
    })

    test('a refused prompt explains how to allow it', async ({ page }) => {
      await page.goto('./#/alerts')
      // Headless Chromium has no prompt to refuse, so answer it the way a "Block" tap would.
      await page.evaluate(() => {
        Notification.requestPermission = async () => 'denied'
      })
      await page.getByRole('button', { name: 'Enable notifications' }).click()
      await expect(page.getByText('Notifications are blocked')).toBeVisible()
    })
  })
})
