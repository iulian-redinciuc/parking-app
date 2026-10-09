import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getLot } from '../api/client'
import type { LatLon } from '../lib/geo'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useProximity } from '../hooks/useProximity'
import { t } from '../i18n'
import { loadPrefs, PREFS_EVENT, proximityText, showLocalNotification } from '../lib/push'

function usePrefs() {
  const [prefs, setPrefs] = useState(loadPrefs)
  useEffect(() => {
    const reload = () => setPrefs(loadPrefs())
    window.addEventListener(PREFS_EVENT, reload)
    return () => window.removeEventListener(PREFS_EVENT, reload)
  }, [])
  return prefs
}

/** The lot's location and default radius, fetched once Tier 1 is switched on. */
function useLotPlace(enabled: boolean) {
  const [place, setPlace] = useState<{ location: LatLon; radiusM: number } | null>(null)
  useEffect(() => {
    if (!enabled || place) return
    let live = true
    getLot()
      .then((lot) => live && setPlace({ location: lot.location, radiusM: lot.notify_radius_m }))
      .catch(() => undefined) // tried again on the next start
    return () => {
      live = false
    }
  }, [enabled, place])
  return place
}

// Tier 1 (notifications.md §3): when *Tell me when I'm near* is on and the phone enters the radius
// while the app is open, a banner at the top of every screen plus a local notification.
export default function ProximityBanner() {
  useTranslation() // re-render on a language change
  const prefs = usePrefs()
  const place = useLotPlace(prefs.proximity)
  const { status } = useLiveStatus()
  const statusRef = useRef(status)
  useEffect(() => {
    statusRef.current = status
  })
  const { alert, dismiss } = useProximity({
    enabled: prefs.proximity,
    lot: place?.location ?? null,
    radiusM: prefs.radius_m ?? place?.radiusM ?? null,
    onAlert: ({ distanceM }) =>
      void showLocalNotification({
        title: t('proximity.title'),
        body: proximityText(distanceM, statusRef.current),
        kind: 'proximity',
      }).catch(() => undefined),
  })
  if (!alert) return null
  return (
    <div
      role="status"
      data-testid="proximity-banner"
      className="mt-3 flex items-start gap-3 rounded-2xl border border-accent bg-surface p-4"
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        className="size-6 shrink-0 text-accent"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21Z" />
        <circle cx="12" cy="9.5" r="2.5" />
      </svg>
      <div className="flex-1">
        <p className="font-semibold">{t('proximity.title')}</p>
        <p className="text-sm">{proximityText(alert.distanceM, status)}</p>
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label={t('proximity.dismiss')}
        className="-m-2 flex size-11 shrink-0 items-center justify-center rounded-full text-muted"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          className="size-5"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
        >
          <path d="M6 6l12 12M18 6 6 18" />
        </svg>
      </button>
    </div>
  )
}
