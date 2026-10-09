import { useCallback, useEffect, useRef, useState } from 'react'
import {
  lastAlertAt,
  locationAllowed,
  PERMISSION_DENIED,
  proximityStep,
  setLastAlertAt,
  type LatLon,
  type ProximityState,
} from '../lib/geo'

/** notifications.md §3 step 2. */
export const WATCH_OPTIONS: PositionOptions = {
  enableHighAccuracy: false,
  maximumAge: 60_000,
  timeout: 30_000,
}

export interface ProximityAlert {
  distanceM: number
  /** `Date.now()` when the radius was entered. */
  at: number
}

export interface ProximityOptions {
  /** *Tell me when I'm near* is on. */
  enabled: boolean
  /** `lot.location` from `GET /api/lot`; `null` until known. */
  lot: LatLon | null
  radiusM: number | null
  /** Called once per alert (the local notification). */
  onAlert?: (alert: ProximityAlert) => void
  geolocation?: Geolocation
  doc?: Document
  allowed?: () => Promise<boolean>
  now?: () => number
}

/**
 * Tier 1 (notifications.md §3): while the page is visible, *Tell me when I'm near* is on and the
 * location permission is granted, watch the position and alert on entering the radius around the
 * lot, at most once per 2 h. The distance is computed here; the position is never sent anywhere.
 * Returns the current alert (for the banner) and a way to hide it.
 */
export function useProximity({
  enabled,
  lot,
  radiusM,
  onAlert,
  geolocation = typeof navigator === 'undefined' ? undefined : navigator.geolocation,
  doc = document,
  allowed = locationAllowed,
  now = Date.now,
}: ProximityOptions): { alert: ProximityAlert | null; dismiss: () => void } {
  const [alert, setAlert] = useState<ProximityAlert | null>(null)
  // Survives hiding the page: driving in while it was hidden still counts as entering.
  const state = useRef<ProximityState>({ inside: null })
  const latest = useRef({ onAlert, allowed, now })
  useEffect(() => {
    latest.current = { onAlert, allowed, now }
  })

  const lat = lot?.lat
  const lon = lot?.lon
  useEffect(() => {
    if (!enabled || lat === undefined || lon === undefined || !radiusM || !geolocation) return
    const target = { lat, lon }
    let watchId: number | null = null
    let active = true

    const onPosition = (pos: GeolocationPosition) => {
      const at = latest.current.now()
      const { latitude, longitude, accuracy } = pos.coords
      const step = proximityStep(
        state.current,
        { lat: latitude, lon: longitude, accuracy },
        target,
        radiusM,
        at,
        lastAlertAt(),
      )
      state.current = step.state
      if (!step.alert) return
      setLastAlertAt(at)
      const next = { distanceM: step.distanceM, at }
      setAlert(next)
      latest.current.onAlert?.(next)
    }
    const stop = () => {
      if (watchId !== null) geolocation.clearWatch(watchId)
      watchId = null
    }
    const onError = (err: GeolocationPositionError) => {
      if (err.code === PERMISSION_DENIED) stop() // taken away meanwhile; timeouts just retry
    }
    const start = async () => {
      if (watchId !== null || doc.visibilityState !== 'visible') return
      const ok = await latest.current.allowed().catch(() => false)
      if (!ok || !active || watchId !== null || doc.visibilityState !== 'visible') return
      watchId = geolocation.watchPosition(onPosition, onError, WATCH_OPTIONS)
    }
    const onVisibility = () => {
      if (doc.visibilityState === 'visible') void start()
      else stop()
    }

    void start()
    doc.addEventListener('visibilitychange', onVisibility)
    return () => {
      active = false
      doc.removeEventListener('visibilitychange', onVisibility)
      stop()
    }
  }, [enabled, lat, lon, radiusM, geolocation, doc])

  const dismiss = useCallback(() => setAlert(null), [])
  return { alert: enabled ? alert : null, dismiss }
}
