import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mockStatus } from '../api/mock'
import {
  cancelOnMyWay,
  DEFAULT_PREFS,
  formatCountdown,
  onMyWayUntil,
  startOnMyWay,
  isSubscribed,
  loadPrefs,
  saveLocalPrefs,
  MOCK_ENDPOINT,
  mockTestBody,
  pushSupport,
  sendTest,
  storedEndpoint,
  SUBSCRIBE_TIMEOUT_MS,
  subscribe,
  unsubscribe,
  updatePrefs,
} from './push'
import * as swPush from './swPush'

const API = 'http://api.test'
const ENDPOINT = 'https://push.example/sub/1'
// A VAPID public key's shape: a 65-byte uncompressed P-256 point, unpadded base64url.
const KEY = btoa(String.fromCharCode(4, ...new Array<number>(64).fill(7)))
  .replace(/=+$/, '')
  .replace(/\+/g, '-')
  .replace(/\//g, '_')
const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'

function json(status: number, body?: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeSubscription(endpoint = ENDPOINT) {
  return {
    endpoint,
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn(() => Promise.resolve(true)),
  }
}

/** A service worker registration whose pushManager hands out `sub`. */
function stubBrowser(permission: NotificationPermission = 'granted') {
  let current: ReturnType<typeof fakeSubscription> | null = null
  const pushManager = {
    subscribe: vi.fn(async () => (current = fakeSubscription())),
    getSubscription: vi.fn(async () => current),
  }
  const reg = { scope: 'http://localhost/', pushManager, showNotification: vi.fn() }
  vi.stubGlobal('navigator', {
    ...navigator,
    serviceWorker: { getRegistration: async () => reg, ready: Promise.resolve(reg) },
  })
  const Notification = Object.assign(function () {}, {
    permission,
    requestPermission: vi.fn(async () => permission),
  })
  vi.stubGlobal('Notification', Notification)
  return { reg, pushManager, Notification, current: () => current }
}

type Call = { url: string; method: string; body: unknown }

function stubFetch(handler: (call: Call) => Response) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const call = {
        url,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(init.body as string) : undefined,
      }
      calls.push(call)
      return handler(call)
    }),
  )
  return calls
}

const api = (call: Call) => {
  if (call.url.endsWith('/vapid-public-key')) return json(200, { key: KEY })
  if (call.url.endsWith('/subscriptions') && call.method === 'POST') return json(201, { id: 'x' })
  if (call.url.endsWith('/subscriptions') && call.method === 'PATCH') return json(200, {})
  if (call.url.endsWith('/subscriptions') && call.method === 'DELETE')
    return new Response(null, { status: 204 })
  if (call.url.endsWith('/test')) return json(200, { sent: true, deleted: false })
  return json(404, { error: { code: 'not_found', message: 'no' } })
}

beforeEach(() => {
  localStorage.clear()
  vi.spyOn(swPush, 'savePushRegistration').mockResolvedValue()
  vi.spyOn(swPush, 'clearPushRegistration').mockResolvedValue()
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('pushSupport', () => {
  it('sends iPhone Safari outside the installed app to the install hint', () => {
    const win = {
      navigator: { userAgent: IPHONE_SAFARI, maxTouchPoints: 5 },
      matchMedia: () => ({ matches: false }),
    } as unknown as Window
    expect(pushSupport(win)).toBe('ios-not-installed')
  })

  it('needs a service worker, PushManager and Notification', () => {
    const win = {
      navigator: { userAgent: 'Mozilla/5.0 (X11; Linux) Chrome/140', maxTouchPoints: 0 },
      matchMedia: () => ({ matches: false }),
    }
    expect(pushSupport(win as unknown as Window)).toBe('unsupported')
    const full = {
      ...win,
      navigator: { ...win.navigator, serviceWorker: {} },
      PushManager: class {},
      Notification: class {},
    }
    expect(pushSupport(full as unknown as Window)).toBe('supported')
  })
})

describe('subscribe (with a server)', () => {
  it('asks permission, subscribes with the VAPID key, POSTs and remembers the endpoint', async () => {
    const browser = stubBrowser()
    const calls = stubFetch(api)
    const prefs = { ...DEFAULT_PREFS, alert_when_almost_full: true }
    await subscribe(prefs, API)

    expect(browser.Notification.requestPermission).toHaveBeenCalledOnce()
    const options = browser.pushManager.subscribe.mock.calls[0] as unknown as [
      PushSubscriptionOptionsInit,
    ]
    expect(options[0].userVisibleOnly).toBe(true)
    expect((options[0].applicationServerKey as Uint8Array).length).toBe(65)
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      `GET ${API}/api/push/vapid-public-key`,
      `POST ${API}/api/push/subscriptions`,
    ])
    expect(calls[1].body).toMatchObject({
      subscription: { endpoint: ENDPOINT, keys: { p256dh: 'p', auth: 'a' } },
      prefs,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      lang: 'en',
    })
    expect(storedEndpoint()).toBe(ENDPOINT)
    expect(loadPrefs()).toEqual(prefs)
    expect(swPush.savePushRegistration).toHaveBeenCalledWith(expect.objectContaining({ prefs }))
    expect(await isSubscribed(API)).toBe(true)
  })

  it('stops at a refused permission without touching the network', async () => {
    stubBrowser('denied')
    const calls = stubFetch(api)
    await expect(subscribe(DEFAULT_PREFS, API)).rejects.toMatchObject({ code: 'denied' })
    expect(calls).toEqual([])
    expect(storedEndpoint()).toBeNull()
  })

  it('reports a server without VAPID keys as unavailable', async () => {
    stubBrowser()
    stubFetch(() => json(503, { error: { code: 'unavailable', message: 'no keys' } }))
    await expect(subscribe(DEFAULT_PREFS, API)).rejects.toMatchObject({ code: 'unavailable' })
    expect(storedEndpoint()).toBeNull()
  })

  it('gives up on a push service that never answers', async () => {
    const browser = stubBrowser()
    stubFetch(api)
    browser.pushManager.subscribe.mockReturnValueOnce(new Promise(() => undefined))
    vi.useFakeTimers()
    try {
      const done = expect(subscribe(DEFAULT_PREFS, API)).rejects.toMatchObject({ code: 'failed' })
      await vi.advanceTimersByTimeAsync(SUBSCRIBE_TIMEOUT_MS)
      await done
    } finally {
      vi.useRealTimers()
    }
    expect(storedEndpoint()).toBeNull()
  })

  it('replaces a subscription made with an older key', async () => {
    const browser = stubBrowser()
    stubFetch(api)
    const old = fakeSubscription('https://push.example/old')
    browser.pushManager.getSubscription.mockResolvedValueOnce(old)
    browser.pushManager.subscribe.mockRejectedValueOnce(
      new DOMException('key', 'InvalidStateError'),
    )
    await subscribe(DEFAULT_PREFS, API)
    expect(old.unsubscribe).toHaveBeenCalledOnce()
    expect(storedEndpoint()).toBe(ENDPOINT)
  })
})

describe('after subscribing (with a server)', () => {
  beforeEach(async () => {
    stubBrowser()
    stubFetch(api)
    await subscribe(DEFAULT_PREFS, API)
  })

  it('PATCHes changed prefs', async () => {
    const calls = stubFetch(api)
    const prefs = { ...DEFAULT_PREFS, schedules: [{ days: [1, 2], time: '08:30' }] }
    await updatePrefs(prefs, API)
    expect(calls).toEqual([
      {
        url: `${API}/api/push/subscriptions`,
        method: 'PATCH',
        body: { endpoint: ENDPOINT, prefs },
      },
    ])
    expect(loadPrefs()).toEqual(prefs)
  })

  it('re-registers when the server forgot this device (404)', async () => {
    const calls = stubFetch((c) =>
      c.method === 'PATCH' ? json(404, { error: { code: 'not_found', message: 'gone' } }) : api(c),
    )
    await updatePrefs({ ...DEFAULT_PREFS, proximity: true }, API)
    expect(calls.map((c) => c.method)).toEqual(['PATCH', 'POST'])
    expect(loadPrefs().proximity).toBe(true)
  })

  it('sends a test push', async () => {
    const calls = stubFetch(api)
    await sendTest(API)
    expect(calls).toEqual([
      { url: `${API}/api/push/test`, method: 'POST', body: { endpoint: ENDPOINT } },
    ])
  })

  it('maps the 3/hour limit to rate_limited', async () => {
    stubFetch(() => json(429, { error: { code: 'rate_limited', message: 'slow down' } }))
    await expect(sendTest(API)).rejects.toMatchObject({ code: 'rate_limited' })
  })

  it('forgets a subscription the push service dropped', async () => {
    stubFetch((c) => (c.url.endsWith('/test') ? json(200, { sent: false, deleted: true }) : api(c)))
    await expect(sendTest(API)).rejects.toMatchObject({ code: 'gone' })
    expect(storedEndpoint()).toBeNull()
  })

  it('unsubscribes on the server and in the browser', async () => {
    const calls = stubFetch(api)
    const sub = await navigator.serviceWorker
      .getRegistration()
      .then((r) => r!.pushManager.getSubscription())
    await unsubscribe(API)
    expect(calls).toEqual([
      { url: `${API}/api/push/subscriptions`, method: 'DELETE', body: { endpoint: ENDPOINT } },
    ])
    expect(sub!.unsubscribe).toHaveBeenCalledOnce()
    expect(storedEndpoint()).toBeNull()
    expect(swPush.clearPushRegistration).toHaveBeenCalled()
    expect(await isSubscribed(API)).toBe(false)
  })

  it('keeps the prefs after unsubscribing (Tier 1 runs without push)', async () => {
    stubFetch(api)
    saveLocalPrefs({ ...DEFAULT_PREFS, proximity: true })
    await unsubscribe(API)
    expect(loadPrefs().proximity).toBe(true)
  })
})

describe('mock mode', () => {
  it('only asks permission, then shows the test notification locally', async () => {
    const browser = stubBrowser()
    const calls = stubFetch(api)
    await subscribe(DEFAULT_PREFS, 'mock')
    expect(storedEndpoint()).toBe(MOCK_ENDPOINT)
    expect(await isSubscribed('mock')).toBe(true)
    await updatePrefs({ ...DEFAULT_PREFS, proximity: true }, 'mock')
    expect(loadPrefs().proximity).toBe(true)
    await sendTest('mock')
    expect(browser.reg.showNotification).toHaveBeenCalledWith(
      'Parking test',
      expect.objectContaining({
        tag: 'parking-status',
        body: expect.stringMatching(/^\d+ free · /),
      }),
    )
    expect(calls).toEqual([])
    expect(browser.pushManager.subscribe).not.toHaveBeenCalled()
  })

  it('is not subscribed once permission is gone', async () => {
    stubBrowser()
    await subscribe(DEFAULT_PREFS, 'mock')
    stubBrowser('default')
    expect(await isSubscribed('mock')).toBe(false)
  })

  it('test body marks flow zones as estimates', () => {
    const status = mockStatus()
    const body = mockTestBody(status)
    expect(body.startsWith(`${status.total.free} free · `)).toBe(true)
    for (const z of status.zones)
      expect(body).toContain(`${z.name} ${z.method === 'flow' ? '≈' : ''}${z.free}`)
  })
})

describe('loadPrefs', () => {
  it('fills in defaults and survives junk', () => {
    expect(loadPrefs()).toEqual(DEFAULT_PREFS)
    localStorage.setItem('parking.pushPrefs', '{"proximity":true}')
    expect(loadPrefs()).toEqual({ ...DEFAULT_PREFS, proximity: true })
    localStorage.setItem('parking.pushPrefs', '{nope')
    expect(loadPrefs()).toEqual(DEFAULT_PREFS)
  })
})

describe("I'm on my way", () => {
  const UNTIL = '2026-10-09T14:35:00.000Z'
  const omw = (c: Call) =>
    c.url.endsWith('/on-my-way')
      ? json(
          202,
          (c.body as { minutes: number }).minutes
            ? { until: UNTIL, sent: true }
            : { until: null, sent: false },
        )
      : api(c)

  beforeEach(async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-09T14:05:00Z'))
    stubBrowser()
    stubFetch(api)
    await subscribe(DEFAULT_PREFS, API)
  })
  afterEach(() => vi.useRealTimers())

  it('starts a window and remembers when it ends', async () => {
    const calls = stubFetch(omw)
    expect(onMyWayUntil()).toBeNull()
    const until = await startOnMyWay(30, API)
    expect(calls).toEqual([
      {
        url: `${API}/api/push/on-my-way`,
        method: 'POST',
        body: { endpoint: ENDPOINT, minutes: 30 },
      },
    ])
    expect(until).toBe(Date.parse(UNTIL))
    expect(onMyWayUntil()).toBe(until)
    vi.setSystemTime(new Date(UNTIL))
    expect(onMyWayUntil()).toBeNull() // over
  })

  it('cancels with minutes 0', async () => {
    const calls = stubFetch(omw)
    await startOnMyWay(15, API)
    await cancelOnMyWay(API)
    expect(calls[1].body).toEqual({ endpoint: ENDPOINT, minutes: 0 })
    expect(onMyWayUntil()).toBeNull()
  })

  it('keeps the window when the cancel fails, drops it when the server forgot the device', async () => {
    stubFetch(omw)
    await startOnMyWay(15, API)
    stubFetch(() => json(503, { error: { code: 'unavailable', message: 'down' } }))
    await expect(cancelOnMyWay(API)).rejects.toMatchObject({ code: 'unavailable' })
    expect(onMyWayUntil()).not.toBeNull()
    stubFetch(() => json(404, { error: { code: 'not_found', message: 'no' } }))
    await cancelOnMyWay(API)
    expect(onMyWayUntil()).toBeNull()
  })

  it('maps errors like the test push', async () => {
    stubFetch(() => json(429, { error: { code: 'rate_limited', message: 'slow down' } }))
    await expect(startOnMyWay(15, API)).rejects.toMatchObject({ code: 'rate_limited' })
    expect(onMyWayUntil()).toBeNull()
  })

  it('turning push off forgets the window', async () => {
    stubFetch(omw)
    await startOnMyWay(60, API)
    await unsubscribe(API)
    expect(onMyWayUntil()).toBeNull()
  })

  it('mock mode shows the first update locally', async () => {
    localStorage.setItem('parking.pushEndpoint', MOCK_ENDPOINT)
    const { reg } = stubBrowser()
    const until = await startOnMyWay(15, 'mock')
    expect(until).toBe(Date.now() + 15 * 60_000)
    expect(reg.showNotification).toHaveBeenCalledOnce()
    const [title] = reg.showNotification.mock.calls[0] as unknown as [string]
    expect(title).toMatch(/^Parking: \d+ free$/)
  })

  it('formats the countdown', () => {
    expect(formatCountdown(15 * 60_000)).toBe('15:00')
    expect(formatCountdown(61_001)).toBe('1:02')
    expect(formatCountdown(-5)).toBe('0:00')
  })
})
