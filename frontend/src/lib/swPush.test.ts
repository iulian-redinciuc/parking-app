import { describe, expect, it, vi } from 'vitest'
import {
  notificationFromPush,
  openFromNotification,
  renewSubscription,
  STATUS_TAG,
  urlBase64ToUint8Array,
  type PushRegistration,
} from './swPush'

const SCOPE = 'https://example.org/parking-app/'

describe('notificationFromPush', () => {
  it('shows the api.md §6 payload with icon, badge, tag and the tap target', () => {
    const payload = {
      title: 'Parking: 23 free',
      body: 'Ground 12 · Underground ≈11 · 17:05',
      tag: 'parking-status',
      url: 'https://example.org/parking-app/#/',
      level: 'plenty',
      kind: 'on_my_way',
    }
    expect(notificationFromPush(JSON.stringify(payload), SCOPE)).toEqual({
      title: 'Parking: 23 free',
      options: {
        body: 'Ground 12 · Underground ≈11 · 17:05',
        tag: 'parking-status',
        renotify: false,
        icon: `${SCOPE}icons/192.png`,
        badge: `${SCOPE}icons/badge-96.png`,
        data: { url: payload.url, kind: 'on_my_way', level: 'plenty' },
      },
    })
  })

  it('shows plain text (DevTools "Push") or an empty push with defaults', () => {
    const text = notificationFromPush('Test push message from DevTools.', SCOPE)
    expect(text.title).toBe('Parking')
    expect(text.options).toMatchObject({
      body: 'Test push message from DevTools.',
      tag: STATUS_TAG,
    })
    expect(text.options.data).toEqual({ url: undefined, kind: undefined, level: undefined })
    expect(notificationFromPush(null, SCOPE)).toMatchObject({
      title: 'Parking',
      options: { body: '', tag: STATUS_TAG, renotify: false },
    })
    expect(notificationFromPush('[1,2]', SCOPE).options.body).toBe('[1,2]')
    expect(notificationFromPush('42', SCOPE).options.body).toBe('42')
  })

  it('ignores fields of the wrong type', () => {
    const n = notificationFromPush(
      JSON.stringify({ title: 5, body: null, tag: '', url: {} }),
      SCOPE,
    )
    expect(n.title).toBe('Parking')
    expect(n.options).toMatchObject({ body: '', tag: STATUS_TAG, data: { url: undefined } })
  })
})

describe('urlBase64ToUint8Array', () => {
  it('decodes unpadded base64url', () => {
    expect([...urlBase64ToUint8Array('-_8')]).toEqual([0xfb, 0xff])
    expect([...urlBase64ToUint8Array('AQID')]).toEqual([1, 2, 3])
    expect(urlBase64ToUint8Array('B'.repeat(87)).length).toBe(65) // a P-256 public key
  })
})

const SAVED: PushRegistration = {
  prefs: { proximity: true, zones: ['ground'] },
  tz: 'Europe/Bucharest',
  lang: 'en',
}

function sub(endpoint: string, key: ArrayBuffer | null = null): PushSubscription {
  return {
    endpoint,
    options: { applicationServerKey: key, userVisibleOnly: true },
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
  } as unknown as PushSubscription
}

function setup(over: Partial<Parameters<typeof renewSubscription>[0]> = {}) {
  const fetch = vi.fn(async (url: string | URL | Request) =>
    String(url).endsWith('/vapid-public-key')
      ? Response.json({ key: 'AQID' })
      : new Response(null, { status: 201 }),
  )
  const subscribe = vi.fn<(options: PushSubscriptionOptionsInit) => Promise<PushSubscription>>(
    async () => sub('https://push.example/new'),
  )
  const deps = {
    pushManager: { subscribe },
    oldSubscription: null,
    newSubscription: null,
    apiBase: 'https://api.example',
    fetch: fetch as unknown as typeof globalThis.fetch,
    load: async () => SAVED,
    ...over,
  }
  return { deps, fetch, subscribe }
}

const calls = (fetch: ReturnType<typeof vi.fn>) =>
  fetch.mock.calls.map(([url, init]) => [
    (init as RequestInit | undefined)?.method ?? 'GET',
    String(url),
    (init as RequestInit | undefined)?.body ? JSON.parse(String((init as RequestInit).body)) : null,
  ])

describe('renewSubscription', () => {
  it('re-subscribes with the old key, registers the new one with saved settings, drops the old', async () => {
    const key = new Uint8Array([9, 9]).buffer
    const { deps, fetch, subscribe } = setup({
      oldSubscription: sub('https://push.example/old', key),
    })
    expect(await renewSubscription(deps)).toBe('renewed')
    expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: key })
    expect(calls(fetch)).toEqual([
      [
        'POST',
        'https://api.example/api/push/subscriptions',
        {
          subscription: { endpoint: 'https://push.example/new', keys: { p256dh: 'p', auth: 'a' } },
          ...SAVED,
        },
      ],
      [
        'DELETE',
        'https://api.example/api/push/subscriptions',
        { endpoint: 'https://push.example/old' },
      ],
    ])
  })

  it('uses the subscription the browser already made; fetches the key when none is known', async () => {
    const given = setup({ newSubscription: sub('https://push.example/given') })
    await renewSubscription(given.deps)
    expect(given.subscribe).not.toHaveBeenCalled()
    expect(calls(given.fetch).map((c) => c[0])).toEqual(['POST'])

    const fresh = setup()
    await renewSubscription(fresh.deps)
    expect(calls(fresh.fetch)[0]).toEqual([
      'GET',
      'https://api.example/api/push/vapid-public-key',
      null,
    ])
    const { applicationServerKey } = fresh.subscribe.mock.calls[0][0]
    expect([...(applicationServerKey as Uint8Array)]).toEqual([1, 2, 3])
  })

  it('does nothing in mock mode or when this device never enabled notifications', async () => {
    for (const over of [{ apiBase: 'mock' }, { load: async () => null }]) {
      const { deps, fetch, subscribe } = setup(over)
      expect(await renewSubscription(deps)).toBe('skipped')
      expect(fetch).not.toHaveBeenCalled()
      expect(subscribe).not.toHaveBeenCalled()
    }
  })

  it('fails when the API refuses the new subscription', async () => {
    const { deps } = setup({
      fetch: (async () => new Response(null, { status: 503 })) as typeof fetch,
      newSubscription: sub('https://push.example/given'),
    })
    await expect(renewSubscription(deps)).rejects.toThrow('HTTP 503')
  })
})

describe('openFromNotification', () => {
  function clients(urls: string[]) {
    const windows = urls.map((url) => ({
      url,
      focus: vi.fn(async () => undefined),
      navigate: vi.fn(async () => null),
    }))
    const api = {
      matchAll: vi.fn(async () => windows),
      openWindow: vi.fn(async () => null),
    }
    return { api: api as unknown as Clients, windows, openWindow: api.openWindow }
  }

  it('focuses an open app window and moves it to the target', async () => {
    const c = clients(['https://other.example/', `${SCOPE}#/stats`])
    await openFromNotification(c.api, SCOPE, { url: `${SCOPE}#/` })
    expect(c.windows[0].focus).not.toHaveBeenCalled()
    expect(c.windows[1].focus).toHaveBeenCalled()
    expect(c.windows[1].navigate).toHaveBeenCalledWith(`${SCOPE}#/`)
    expect(c.openWindow).not.toHaveBeenCalled()
  })

  it('only focuses when the window is already there', async () => {
    const c = clients([`${SCOPE}#/`])
    await openFromNotification(c.api, SCOPE, { url: `${SCOPE}#/` })
    expect(c.windows[0].focus).toHaveBeenCalled()
    expect(c.windows[0].navigate).not.toHaveBeenCalled()
  })

  it('opens the app when no window is open, never a foreign url', async () => {
    const c = clients([])
    await openFromNotification(c.api, SCOPE, { url: 'https://evil.test/' })
    expect(c.openWindow).toHaveBeenCalledWith(SCOPE)
  })
})
