// Tier 1 proximity (notifications.md §3): distance to the lot and when an approach counts. Pure
// functions; the watching is `hooks/useProximity.ts`. The position never leaves the phone.

export interface LatLon {
  lat: number
  lon: number
}

/** Mean Earth radius (IUGG), metres. */
export const EARTH_RADIUS_M = 6_371_008.8

const rad = (deg: number) => (deg * Math.PI) / 180

/** Great-circle distance in metres (haversine). */
export function haversineM(a: LatLon, b: LatLon): number {
  const dLat = rad(b.lat - a.lat)
  const dLon = rad(b.lon - a.lon)
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)))
}

/** Readings less precise than this are ignored. */
export const MAX_ACCURACY_M = 1000
/** No second alert for this long after one (kept in `localStorage`). */
export const PROXIMITY_COOLDOWN_MS = 2 * 60 * 60_000

export interface Reading extends LatLon {
  accuracy: number
}

export interface ProximityState {
  /** Whether the last usable reading was inside the radius; `null` before the first one. */
  inside: boolean | null
}

export type ProximityStep =
  | { state: ProximityState; alert: false }
  | { state: ProximityState; alert: true; distanceM: number }

/**
 * One position reading: alert on entering the radius (the previous usable reading was outside, or
 * there was none) unless an alert was given less than the cooldown ago. Readings with an accuracy
 * worse than 1000 m change nothing.
 */
export function proximityStep(
  state: ProximityState,
  reading: Reading,
  lot: LatLon,
  radiusM: number,
  now: number,
  lastAlertAt: number | null,
): ProximityStep {
  if (!(reading.accuracy <= MAX_ACCURACY_M)) return { state, alert: false }
  const distanceM = haversineM(reading, lot)
  const inside = distanceM < radiusM
  const next = { inside }
  const cooling = lastAlertAt !== null && now - lastAlertAt < PROXIMITY_COOLDOWN_MS
  if (inside && state.inside !== true && !cooling) return { state: next, alert: true, distanceM }
  return { state: next, alert: false }
}

/** A distance as shown: rounded to 10 m below 1 km, else to 0.1 km. */
export function roundDistance(distanceM: number): { unit: 'm' | 'km'; value: number } {
  if (distanceM < 995) return { unit: 'm', value: Math.max(10, Math.round(distanceM / 10) * 10) }
  return { unit: 'km', value: Math.round(distanceM / 100) / 10 }
}

// --- browser: the location permission ---

const LAST_ALERT = 'parking.proximityAlertAt'
const ALLOWED = 'parking.locationAllowed'
/** `GeolocationPositionError.PERMISSION_DENIED`. */
export const PERMISSION_DENIED = 1

function getItem(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null // private mode
  }
}

function setItem(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // private mode: this visit only
  }
}

export function lastAlertAt(): number | null {
  const value = Number(getItem(LAST_ALERT))
  return Number.isFinite(value) && value > 0 ? value : null
}

export function setLastAlertAt(at: number) {
  setItem(LAST_ALERT, String(at))
}

/**
 * Whether the location permission is granted, without prompting. Where the Permissions API can't
 * say (older Safari), whether `requestLocation` succeeded on this device before.
 */
export async function locationAllowed(nav: Navigator = navigator): Promise<boolean> {
  try {
    const status = await nav.permissions.query({ name: 'geolocation' })
    return status.state === 'granted'
  } catch {
    return getItem(ALLOWED) === '1'
  }
}

/**
 * Asks for the location permission (one position fix). **Call from a tap** (*Tell me when I'm
 * near*). False only when the user refused or there is no geolocation at all.
 */
export function requestLocation(geo: Geolocation | undefined = navigator.geolocation) {
  return new Promise<boolean>((resolve) => {
    if (!geo) {
      resolve(false)
      return
    }
    geo.getCurrentPosition(
      () => {
        setItem(ALLOWED, '1')
        resolve(true)
      },
      (err) => {
        // No fix yet (timeout, no signal) still means the permission was given.
        const ok = err.code !== PERMISSION_DENIED
        setItem(ALLOWED, ok ? '1' : '0')
        resolve(ok)
      },
      { enableHighAccuracy: false, maximumAge: 60_000, timeout: 30_000 },
    )
  })
}
