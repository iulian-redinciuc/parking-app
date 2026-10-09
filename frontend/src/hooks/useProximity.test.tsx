import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ProximityBanner from '../components/ProximityBanner'
import { PROXIMITY_COOLDOWN_MS } from '../lib/geo'
import { PREFS_EVENT } from '../lib/push'
import { useProximity, WATCH_OPTIONS } from './useProximity'

const LOT = { lat: 51.5007, lon: -0.1246 } // the mock lot (api/__fixtures__/lot-info.json)
const NEAR = { latitude: 51.5043, longitude: -0.1246, accuracy: 30 } // ≈ 400 m north
const FAR = { latitude: 51.5207, longitude: -0.1246, accuracy: 30 } // ≈ 2.2 km north

/** A `navigator.geolocation` whose position the test moves. */
function fakeGeolocation() {
  let watcher: { ok: PositionCallback; fail?: PositionErrorCallback | null } | null = null
  const geo = {
    watchPosition: vi.fn<Geolocation['watchPosition']>((ok, fail) => {
      watcher = { ok, fail }
      return 7
    }),
    clearWatch: vi.fn(() => {
      watcher = null
    }),
    getCurrentPosition: vi.fn(),
  }
  const moveTo = (coords: { latitude: number; longitude: number; accuracy: number }) =>
    act(() => watcher?.ok({ coords, timestamp: Date.now() } as GeolocationPosition))
  const fail = (code: number) => act(() => watcher?.fail?.({ code } as GeolocationPositionError))
  return { geo, moveTo, fail, watching: () => watcher !== null }
}

/** A document whose visibility the test flips. */
function fakeDocument() {
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const setVisible = (visible: boolean) =>
    act(() => {
      doc.visibilityState = visible ? 'visible' : 'hidden'
      doc.dispatchEvent(new Event('visibilitychange'))
    })
  return { doc: doc as unknown as Document, setVisible }
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function setup(overrides: { allowed?: boolean; enabled?: boolean } = {}) {
  const g = fakeGeolocation()
  const d = fakeDocument()
  let now = 1_000_000_000_000
  const onAlert = vi.fn()
  const hook = renderHook(
    ({ enabled }) =>
      useProximity({
        enabled,
        lot: LOT,
        radiusM: 500,
        onAlert,
        geolocation: g.geo as unknown as Geolocation,
        doc: d.doc,
        allowed: async () => overrides.allowed ?? true,
        now: () => now,
      }),
    { initialProps: { enabled: overrides.enabled ?? true } },
  )
  const advance = (ms: number) => (now += ms)
  return { ...g, ...d, hook, onAlert, advance }
}

describe('useProximity', () => {
  it('outside → inside → banner once → cooldown', async () => {
    const { geo, moveTo, hook, onAlert, advance } = setup()
    await waitFor(() => expect(geo.watchPosition).toHaveBeenCalledOnce())
    expect(geo.watchPosition.mock.calls[0][2]).toEqual(WATCH_OPTIONS)

    moveTo(FAR)
    expect(hook.result.current.alert).toBeNull()
    moveTo(NEAR)
    expect(hook.result.current.alert?.distanceM).toBeCloseTo(400, -1)
    expect(onAlert).toHaveBeenCalledOnce()

    // Staying inside: no repeat.
    moveTo({ ...NEAR, latitude: 51.504 })
    expect(onAlert).toHaveBeenCalledOnce()

    // Out and back in within 2 h: no repeat (the cooldown is stored).
    act(() => hook.result.current.dismiss())
    expect(hook.result.current.alert).toBeNull()
    advance(30 * 60_000)
    moveTo(FAR)
    moveTo(NEAR)
    expect(onAlert).toHaveBeenCalledOnce()
    expect(hook.result.current.alert).toBeNull()

    // After 2 h: entering alerts again.
    advance(PROXIMITY_COOLDOWN_MS)
    moveTo(FAR)
    moveTo(NEAR)
    expect(onAlert).toHaveBeenCalledTimes(2)
    expect(hook.result.current.alert).not.toBeNull()
  })

  it('alerts when the first reading is already inside, ignoring vague ones', async () => {
    const { geo, moveTo, onAlert } = setup()
    await waitFor(() => expect(geo.watchPosition).toHaveBeenCalled())
    moveTo({ ...NEAR, accuracy: 1500 })
    expect(onAlert).not.toHaveBeenCalled()
    moveTo(NEAR)
    expect(onAlert).toHaveBeenCalledOnce()
  })

  it('keeps the cooldown across reloads', async () => {
    const first = setup()
    await waitFor(() => expect(first.geo.watchPosition).toHaveBeenCalled())
    first.moveTo(NEAR)
    expect(first.onAlert).toHaveBeenCalledOnce()
    first.hook.unmount()

    const second = setup()
    await waitFor(() => expect(second.geo.watchPosition).toHaveBeenCalled())
    second.moveTo(NEAR)
    expect(second.onAlert).not.toHaveBeenCalled()
  })

  it('watches only while visible, and keeps the last reading meanwhile', async () => {
    const { geo, moveTo, setVisible, watching, onAlert } = setup()
    await waitFor(() => expect(watching()).toBe(true))
    moveTo(FAR)
    setVisible(false)
    expect(geo.clearWatch).toHaveBeenCalledWith(7)
    expect(watching()).toBe(false)
    setVisible(true)
    await waitFor(() => expect(geo.watchPosition).toHaveBeenCalledTimes(2))
    moveTo(NEAR)
    expect(onAlert).toHaveBeenCalledOnce()
  })

  it('does nothing when switched off or without the location permission', async () => {
    const off = setup({ enabled: false })
    const denied = setup({ allowed: false })
    await act(async () => undefined)
    expect(off.geo.watchPosition).not.toHaveBeenCalled()
    expect(denied.geo.watchPosition).not.toHaveBeenCalled()
  })

  it('stops when the permission is taken away, and when switched off', async () => {
    const a = setup()
    await waitFor(() => expect(a.watching()).toBe(true))
    a.fail(3) // timeout: keeps watching
    expect(a.watching()).toBe(true)
    a.fail(1)
    expect(a.watching()).toBe(false)

    const b = setup()
    await waitFor(() => expect(b.watching()).toBe(true))
    b.hook.rerender({ enabled: false })
    expect(b.watching()).toBe(false)
  })
})

describe('ProximityBanner', () => {
  it('shows the banner and a local notification once the lot is near', async () => {
    const g = fakeGeolocation()
    const reg = { scope: 'http://localhost/', showNotification: vi.fn(async () => undefined) }
    vi.stubGlobal('navigator', {
      ...navigator,
      geolocation: g.geo,
      permissions: { query: async () => ({ state: 'granted' }) },
      serviceWorker: { getRegistration: async () => reg },
    })
    vi.stubGlobal('Notification', { permission: 'granted' })
    render(<ProximityBanner />)
    expect(g.geo.watchPosition).not.toHaveBeenCalled() // off by default

    act(() => {
      localStorage.setItem('parking.pushPrefs', JSON.stringify({ proximity: true }))
      window.dispatchEvent(new Event(PREFS_EVENT))
    })
    await waitFor(() => expect(g.watching()).toBe(true))
    g.moveTo(NEAR)

    const banner = await screen.findByTestId('proximity-banner')
    expect(banner).toHaveTextContent("You're near the parking")
    expect(banner).toHaveTextContent(
      /You're 400 m away · \d+ free \(Ground \d+ · Underground ≈\d+\)/,
    )
    await waitFor(() => expect(reg.showNotification).toHaveBeenCalledOnce())
    const [title, options] = reg.showNotification.mock.calls[0] as unknown as [
      string,
      NotificationOptions,
    ]
    expect(title).toBe("You're near the parking")
    expect(options.tag).toBe('parking-status')
    expect(options.body).toMatch(/^You're 400 m away · \d+ free/)

    act(() => screen.getByRole('button', { name: 'Hide this alert' }).click())
    expect(screen.queryByTestId('proximity-banner')).toBeNull()
  })
})
