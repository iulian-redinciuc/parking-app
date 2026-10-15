import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  type APIRequestContext,
  type BrowserContext,
  type BrowserContextOptions,
  type LaunchOptions,
  type Page,
  test as base,
  chromium,
  expect,
} from '@playwright/test'

// P6.8: the device test matrix of notifications.md §6, live (see playwright.devices.config.ts).
// The ground camera is played by posting observations to the API the way the worker does.
const API = process.env.DM_API_URL ?? 'http://127.0.0.1:8090'
const TOKEN = process.env.DM_WORKER_TOKEN ?? ''
const SLOTS = Array.from({ length: 17 }, (_, i) => `G${String(i + 1).padStart(2, '0')}`)
// The lot is at LOT_LAT/LOT_LON = 51.5007, -0.1246 (the runner sets them), radius 500 m.
const FAR = { latitude: 51.5207, longitude: -0.1246, accuracy: 30 }
const NEAR = { latitude: 51.5043, longitude: -0.1246, accuracy: 30 }

type Row = { push: boolean; tier1: boolean; standalone: boolean; installHint: boolean }
// The "expected" cells of the matrix, per project.
const ROWS: Record<string, Row> = {
  'Android Chrome': { push: true, tier1: true, standalone: false, installHint: false },
  'iPhone installed': { push: true, tier1: true, standalone: true, installHint: false },
  'iPhone Safari': { push: false, tier1: true, standalone: false, installHint: true },
  'Desktop Chrome': { push: true, tier1: false, standalone: false, installHint: false },
}

// Chromium has no Push API in incognito, which is what Playwright's contexts are, so each project
// (= device) gets one persistent profile with its device settings, kept for all its tests like a
// phone keeps its browser. (A fresh profile per test meant a new FCM registration each time; FCM
// dropped some of those within seconds: 410 on the next push.) Each test starts from a reset:
// no permissions, no saved settings, no push subscription.
const test = base.extend<object, { device: BrowserContext }>({
  device: [
    // eslint-disable-next-line no-empty-pattern -- Playwright fixtures destructure their deps
    async ({}, provide, info) => {
      const { launchOptions, channel, ...options } = info.project.use as BrowserContextOptions & {
        launchOptions?: LaunchOptions
        channel?: string
      }
      const dir = await mkdtemp(join(tmpdir(), 'parking-dm-profile-'))
      const device = await chromium.launchPersistentContext(dir, {
        ...options,
        ...launchOptions,
        channel,
        headless: true,
      })
      await provide(device)
      await device.close()
      await rm(dir, { recursive: true, force: true })
    },
    { scope: 'worker' },
  ],
  context: async ({ device }, provide) => {
    await device.clearPermissions()
    await device.setGeolocation(null)
    await provide(device)
  },
  page: async ({ context }, provide, info) => {
    const page = await context.newPage()
    if (ROWS[info.project.name].standalone)
      // What Safari sets in an app opened from the home screen.
      await page.addInitScript(() =>
        Object.defineProperty(Navigator.prototype, 'standalone', { get: () => true }),
      )
    await page.goto('./#/')
    const endpoint = await page.evaluate(async () => {
      localStorage.clear()
      const reg = await navigator.serviceWorker.ready
      for (const n of await reg.getNotifications()) n.close()
      const sub = await reg.pushManager.getSubscription()
      await sub?.unsubscribe()
      return sub?.endpoint ?? null
    })
    // A test that failed half-way leaves its subscription on the server (maybe in an on-my-way
    // window); the same profile can get the same endpoint back, so forget it there too.
    if (endpoint) await page.request.delete(`${API}/api/push/subscriptions`, { data: { endpoint } })
    await provide(page)
    await page.close()
  },
})

let row: Row
// eslint-disable-next-line no-empty-pattern -- Playwright fixtures destructure their deps
test.beforeEach(({}, info) => {
  row = ROWS[info.project.name]
})

/**
 * Sets the ground's free spaces, like cam-ground's analysed frames. Three frames: the API takes a
 * space's change after 3 frames in a row (SlotSmoother, vision.md).
 */
async function groundFree(request: APIRequestContext, free: number): Promise<void> {
  const taken = new Set(SLOTS.slice(0, SLOTS.length - free))
  for (let frame = 0; frame < 3; frame++) {
    const r = await request.post(`${API}/internal/observations`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      data: {
        v: 1,
        camera_id: 'cam-ground',
        ts: new Date().toISOString(),
        frame_size: [10, 10],
        inference_ms: 5,
        detections: taken.size,
        slots: SLOTS.map((id) => ({ id, score: taken.has(id) ? 0.9 : 0, taken: taken.has(id) })),
      },
    })
    expect(r.status()).toBe(204)
  }
}

type Shown = { title: string; body: string; kind: string | null }

async function shown(page: Page): Promise<Shown[]> {
  return page.evaluate(async () =>
    (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => ({
      title: n.title,
      body: n.body,
      kind: (n.data as { kind?: string } | null)?.kind ?? null,
    })),
  )
}

/**
 * Every notification shown from now on, in order. They share one tag, so each replaces the last
 * and `getNotifications()` holds only the newest; the page watches it and keeps the history.
 */
async function recordNotifications(page: Page): Promise<() => Promise<Shown[]>> {
  await page.evaluate(() => {
    const w = window as unknown as { __shown: string[] }
    w.__shown = []
    const since = Date.now()
    const poll = async () => {
      for (const n of await (await navigator.serviceWorker.ready).getNotifications()) {
        if (n.timestamp < since) continue // shown before this check started
        const key = JSON.stringify({
          title: n.title,
          body: n.body,
          kind: (n.data as { kind?: string } | null)?.kind ?? null,
          ts: n.timestamp,
        })
        if (!w.__shown.includes(key)) w.__shown.push(key)
      }
      setTimeout(poll, 100)
    }
    void poll()
  })
  return async () =>
    (await page.evaluate(() => (window as unknown as { __shown: string[] }).__shown)).map((s) => {
      const { title, body, kind } = JSON.parse(s) as Shown
      return { title, body, kind }
    })
}

/** Alerts → Enable notifications, checking that nothing was asked before the tap. */
async function enablePush(page: Page): Promise<void> {
  await page.goto('./#/alerts')
  await page.evaluate(() => navigator.serviceWorker.ready)
  expect(await page.evaluate(() => Notification.permission)).toBe('default')
  expect(
    await page.evaluate(async () =>
      (await navigator.serviceWorker.ready).pushManager.getSubscription(),
    ),
  ).toBeNull()
  await page.context().grantPermissions(['notifications'])
  // A fresh profile's first registration with FCM can take longer than the app waits (30 s) on
  // the dev Pi; the app then says so and the user taps again. Retries are noted in the report.
  const on = page.getByText('Notifications are on for this device.')
  const failed = page.getByText("That didn't work. Check your connection and try again.")
  for (let attempt = 1; ; attempt++) {
    await page.getByRole('button', { name: 'Enable notifications' }).click()
    // "Too many tries" = the API's push budget (scripts/device-matrix.sh restarts it per device).
    const limited = page.getByText('Too many tries. Please wait a while and try again.')
    await expect(on.or(failed).or(limited)).toBeVisible({ timeout: 45_000 })
    if (await on.isVisible()) break
    expect(await limited.isVisible(), 'rate limited by the API').toBe(false)
    expect(attempt, 'enable failed 3 times').toBeLessThan(3)
    test.info().annotations.push({ type: 'retry', description: `enable tapped again (${attempt})` })
  }
}

/** "HH:MM" in the browser's time zone, `minutes` from now. */
function clock(page: Page, minutes: number): Promise<string> {
  return page.evaluate((m) => {
    const d = new Date(Date.now() + m * 60_000)
    return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
  }, minutes)
}

async function waitUntilSecond(page: Page, at: number): Promise<void> {
  const s = new Date().getSeconds()
  if (s > at) await page.waitForTimeout((60 - s + 2) * 1000)
}

test('push: no prompt before the tap; enable → test notification arrives', async ({ page }) => {
  await groundFree(page.request, 12)
  if (!row.push) {
    // iPhone Safari outside the installed app: the install hint, no button, nothing asked.
    await page.goto('./#/alerts')
    await expect(page.getByTestId('install-hint')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Enable notifications' })).toHaveCount(0)
    expect(await page.evaluate(() => Notification.permission)).toBe('default')
    return
  }
  await page.goto('./#/alerts')
  await expect(page.getByRole('button', { name: 'Enable notifications' })).toBeVisible()
  await expect(page.getByTestId('install-hint')).toHaveCount(0)
  await enablePush(page)
  await page.getByRole('button', { name: 'Send test notification' }).click()
  // The API answers once the push service has (FCM took over 5 s at times from the dev Pi).
  await expect(page.getByText(/It should arrive in a few seconds/)).toBeVisible({
    timeout: 30_000,
  })
  await expect
    .poll(() => shown(page), { timeout: 30_000 })
    .toEqual([
      { title: 'Parking: 72 free', body: expect.stringMatching(/^Ground 12 · /), kind: 'test' },
    ])
  await page.getByRole('button', { name: 'Turn off notifications' }).click()
  await expect(page.getByRole('button', { name: 'Enable notifications' })).toBeVisible()
})

test('tier 1: entering the radius with the app open shows the banner', async ({ page }) => {
  test.skip(!row.tier1, 'n/a on desktop')
  await groundFree(page.request, 12)
  const context = page.context()
  // A phone with push asks for notifications from the Alerts screen first; Safari outside the
  // installed app has none, so it gets the banner only.
  await context.grantPermissions(row.push ? ['geolocation', 'notifications'] : ['geolocation'])
  await context.setGeolocation(FAR)
  await page.goto('./#/alerts')
  await page.evaluate(() => navigator.serviceWorker.ready)
  // The switch turns on once the location request (from the tap) has answered.
  const near = page.getByRole('switch', { name: /Tell me when I'm near/ })
  await near.click()
  await expect(near).toBeChecked()
  await page.goto('./#/')
  await expect(page.getByTestId('big-count')).toBeVisible()
  await expect(page.getByTestId('proximity-banner')).toHaveCount(0)

  await context.setGeolocation(NEAR)
  const banner = page.getByTestId('proximity-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText(/You're 400 m away · 72 free \(Ground 12 · Underground ≈60\)/)
  const expected = row.push ? [{ title: "You're near the parking", kind: 'proximity' }] : []
  await expect
    .poll(async () => (await shown(page)).map(({ title, kind }) => ({ title, kind })))
    .toEqual(expected)

  // Out and back in after a reload: not again within 2 h.
  await banner.getByRole('button', { name: 'Hide this alert' }).click()
  await context.setGeolocation(FAR)
  await page.reload()
  await context.setGeolocation(NEAR)
  await page.waitForTimeout(2000)
  await expect(page.getByTestId('proximity-banner')).toHaveCount(0)
})

test("tier 2: I'm on my way → now, on a big change, not after Stop", async ({ page }) => {
  test.skip(!row.push, 'no push in Safari outside the installed app')
  await groundFree(page.request, 12)
  await enablePush(page)
  const history = await recordNotifications(page)
  await page.getByRole('button', { name: '15 min' }).click()
  await expect(page.getByRole('timer')).toHaveText(/^1[45]:\d\d left$/)
  await expect
    .poll(history, { timeout: 30_000 })
    .toEqual([{ title: 'Parking: 72 free', body: expect.any(String), kind: 'on_my_way' }])

  // A small change: nothing. The ground filling up: a push at once (no 2 min gap for "full").
  await groundFree(page.request, 11)
  await page.waitForTimeout(3000)
  expect(await history()).toHaveLength(1)
  await groundFree(page.request, 0)
  await expect
    .poll(async () => (await history()).map((n) => n.title), { timeout: 30_000 })
    .toEqual(['Parking: 72 free', 'Parking: 60 free'])

  await page.getByRole('button', { name: 'Stop updates' }).click()
  await expect(page.getByRole('button', { name: '30 min' })).toBeVisible()
  await groundFree(page.request, 17)
  await page.waitForTimeout(5000)
  expect(await history()).toHaveLength(2)
  await page.getByRole('button', { name: 'Turn off notifications' }).click()
})

test('tier 3: almost full warns once', async ({ page }) => {
  test.skip(!row.push, 'no push in Safari outside the installed app')
  await groundFree(page.request, 12)
  await enablePush(page)
  await page.getByRole('switch', { name: /Warn when almost full/ }).check()
  await expect(page.getByText('Saved')).toBeVisible()
  const history = await recordNotifications(page)
  await groundFree(page.request, 0)
  await expect
    .poll(history, { timeout: 30_000 })
    .toEqual([{ title: 'Parking: 60 free', body: expect.any(String), kind: 'almost_full' }])
  // Free again and full again: not within the 2 h cooldown.
  await groundFree(page.request, 12)
  await groundFree(page.request, 0)
  await page.waitForTimeout(5000)
  expect(await history()).toHaveLength(1)
  await groundFree(page.request, 12)
  await page.getByRole('button', { name: 'Turn off notifications' }).click()
})

test('tier 3: a reminder fires at its minute; none inside quiet hours', async ({ page }) => {
  test.skip(!row.push, 'no push in Safari outside the installed app')
  test.setTimeout(300_000)
  await groundFree(page.request, 12)
  await enablePush(page)
  const history = await recordNotifications(page)
  // Leave time to save before the first minute starts.
  await waitUntilSecond(page, 40)
  const [quiet, due, after] = [await clock(page, 1), await clock(page, 2), await clock(page, 3)]
  // Quiet hours = the first minute only (`to` is excluded): the reminder then is skipped, the
  // one a minute later is sent.
  await page.getByRole('switch', { name: /Quiet hours/ }).check()
  await page.getByLabel('From').fill(quiet)
  await page.getByLabel('To').fill(due)
  const days = page.getByRole('group', { name: 'Days' }).getByRole('button')
  for (const day of await days.all())
    if ((await day.getAttribute('aria-pressed')) !== 'true') await day.click()
  for (const time of [quiet, due]) {
    await page.getByLabel('Time', { exact: true }).fill(time)
    await page.getByRole('button', { name: 'Add reminder' }).click()
  }
  await expect(page.getByText('Saved')).toBeVisible()
  await expect(page.getByRole('list', { name: 'Reminders' }).getByRole('listitem')).toHaveCount(2)

  // Keep the ground camera fresh while waiting (stale after 60 s).
  const keepFresh = setInterval(() => void groundFree(page.request, 12).catch(() => {}), 20_000)
  try {
    await expect
      .poll(history, { timeout: 200_000, intervals: [1000] })
      .toEqual([{ title: 'Parking: 72 free', body: expect.any(String), kind: 'schedule' }])
    const sentAt = await clock(page, 0)
    expect([due, after]).toContain(sentAt) // within the minute after the reminder's start
    await page.waitForTimeout(5000)
    expect(await history()).toHaveLength(1)
  } finally {
    clearInterval(keepFresh)
  }
  await page.getByRole('button', { name: 'Turn off notifications' }).click()
})
