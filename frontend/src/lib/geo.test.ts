import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  haversineM,
  lastAlertAt,
  locationAllowed,
  PROXIMITY_COOLDOWN_MS,
  proximityStep,
  requestLocation,
  roundDistance,
  setLastAlertAt,
} from './geo'

const LONDON = { lat: 51.5074, lon: -0.1278 }
const PARIS = { lat: 48.8566, lon: 2.3522 }
const NEW_YORK = { lat: 40.7128, lon: -74.006 }
const SYDNEY = { lat: -33.8688, lon: 151.2093 }
const BUCHAREST = { lat: 44.4268, lon: 26.1025 }
const CLUJ = { lat: 46.7712, lon: 23.6236 }

describe('haversineM', () => {
  it.each([
    ['London–Paris', LONDON, PARIS, 343.6],
    ['London–New York', LONDON, NEW_YORK, 5570.2],
    ['Bucharest–Cluj', BUCHAREST, CLUJ, 324.0],
    ['New York–Sydney', NEW_YORK, SYDNEY, 15988.8],
  ])('%s', (_name, a, b, km) => {
    expect(haversineM(a, b) / 1000).toBeCloseTo(km, 0)
    expect(haversineM(b, a)).toBeCloseTo(haversineM(a, b), 6)
  })

  it('is 0 for the same point and ~111.2 km per degree of latitude', () => {
    expect(haversineM(LONDON, LONDON)).toBe(0)
    expect(haversineM({ lat: 0, lon: 0 }, { lat: 1, lon: 0 })).toBeCloseTo(111_195, -1)
  })

  it('handles antipodes and the date line', () => {
    expect(haversineM({ lat: 0, lon: 0 }, { lat: 0, lon: 180 })).toBeCloseTo(
      Math.PI * 6_371_008.8,
      0,
    )
    expect(haversineM({ lat: 0, lon: 179.999 }, { lat: 0, lon: -179.999 })).toBeLessThan(250)
  })
})

describe('proximityStep', () => {
  const LOT = { lat: 51.5007, lon: -0.1246 }
  // ~0.0036° of latitude ≈ 400 m north of the lot; ~0.009° ≈ 1 km
  const near = { lat: 51.5043, lon: -0.1246, accuracy: 30 }
  const far = { lat: 51.5097, lon: -0.1246, accuracy: 30 }
  const T = 1_000_000_000

  it('alerts on the first reading inside the radius', () => {
    const step = proximityStep({ inside: null }, near, LOT, 500, T, null)
    expect(step.alert).toBe(true)
    if (step.alert) expect(step.distanceM).toBeCloseTo(400, -1)
    expect(step.state).toEqual({ inside: true })
  })

  it('alerts on entering, not while staying inside', () => {
    const out = proximityStep({ inside: null }, far, LOT, 500, T, null)
    expect(out).toEqual({ state: { inside: false }, alert: false })
    expect(proximityStep(out.state, near, LOT, 500, T, null).alert).toBe(true)
    expect(proximityStep({ inside: true }, near, LOT, 500, T, null).alert).toBe(false)
  })

  it('stays quiet within 2 h of the last alert', () => {
    expect(proximityStep({ inside: false }, near, LOT, 500, T, T - 60_000).alert).toBe(false)
    const later = T - PROXIMITY_COOLDOWN_MS
    expect(proximityStep({ inside: false }, near, LOT, 500, T, later).alert).toBe(true)
  })

  it('ignores readings less accurate than 1000 m', () => {
    const vague = { ...near, accuracy: 1500 }
    expect(proximityStep({ inside: false }, vague, LOT, 500, T, null)).toEqual({
      state: { inside: false },
      alert: false,
    })
    expect(
      proximityStep({ inside: false }, { ...near, accuracy: 1000 }, LOT, 500, T, null).alert,
    ).toBe(true)
  })
})

describe('roundDistance', () => {
  it.each([
    [0, { unit: 'm', value: 10 }],
    [404, { unit: 'm', value: 400 }],
    [994, { unit: 'm', value: 990 }],
    [995, { unit: 'km', value: 1 }],
    [1240, { unit: 'km', value: 1.2 }],
  ])('%d m', (m, expected) => expect(roundDistance(m)).toEqual(expected))
})

describe('location permission', () => {
  beforeEach(() => localStorage.clear())

  it('reads the Permissions API, else what requestLocation found', async () => {
    const granted = { permissions: { query: async () => ({ state: 'granted' }) } }
    expect(await locationAllowed(granted as unknown as Navigator)).toBe(true)
    const prompt = { permissions: { query: async () => ({ state: 'prompt' }) } }
    expect(await locationAllowed(prompt as unknown as Navigator)).toBe(false)
    const none = {} as Navigator
    expect(await locationAllowed(none)).toBe(false)
    const geo = { getCurrentPosition: (ok: PositionCallback) => ok({} as GeolocationPosition) }
    expect(await requestLocation(geo as unknown as Geolocation)).toBe(true)
    expect(await locationAllowed(none)).toBe(true)
  })

  it('a refusal is false, a timeout still counts as allowed', async () => {
    const failWith = (code: number) => ({
      getCurrentPosition: vi.fn((_: PositionCallback, fail: PositionErrorCallback) =>
        fail({ code } as GeolocationPositionError),
      ),
    })
    expect(await requestLocation(failWith(1) as unknown as Geolocation)).toBe(false)
    expect(await requestLocation(failWith(3) as unknown as Geolocation)).toBe(true)
    expect(await requestLocation(undefined)).toBe(false)
  })

  it('keeps the last alert time', () => {
    expect(lastAlertAt()).toBeNull()
    setLastAlertAt(123)
    expect(lastAlertAt()).toBe(123)
  })
})
