import { useTranslation } from 'react-i18next'
import type { ZoneStatus } from '../api/types'
import { TONE_TEXT, formatFree, isEstimated, levelInfo, specialSpaces } from '../lib/status'
import LevelBar from './LevelBar'
import SlotMap from './SlotMap'
import TrendIcon from './TrendIcon'

// One zone: name, free / capacity, trend, a bar with the level word, a chip per kind of special
// space (accessible, EV charging…, P9.2), a note when estimated, and the slot map where the zone
// has one (P9.1).
// `dimmed` greys the numbers: they're old (stale zone, or the connection is down).
export default function ZoneCard({ zone, dimmed = false }: { zone: ZoneStatus; dimmed?: boolean }) {
  const { t } = useTranslation()
  const level = levelInfo(zone.level)
  const nameId = `zone-${zone.id}`
  const special = specialSpaces(zone)
  return (
    <li
      className={`flex flex-col gap-2 rounded-xl bg-surface p-4 ${dimmed ? 'dimmed' : ''}`}
      aria-labelledby={nameId}
      data-testid={`zone-${zone.id}`}
      data-dimmed={dimmed || undefined}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 id={nameId} className="truncate text-lg font-semibold">
          {zone.name}
        </h2>
        <span className="flex shrink-0 items-center gap-2">
          <span className="text-lg font-semibold tabular-nums">
            {formatFree(zone)}
            <span className="font-normal text-muted"> / {zone.capacity}</span>
            <span className="sr-only"> {t('zone.free')}</span>
          </span>
          <TrendIcon trend={zone.trend} />
        </span>
      </div>
      <div className="flex items-center gap-3">
        <LevelBar level={zone.level} capacity={zone.capacity} occupied={zone.occupied} />
        <span className={`shrink-0 text-sm font-semibold ${TONE_TEXT[level.tone]}`}>
          {level.label}
        </span>
      </div>
      {special.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label={t('special.list')}>
          {special.map((s) => (
            <span
              key={s.type}
              className="rounded-full border border-muted/40 px-2.5 py-0.5 text-sm"
              data-testid={`special-${s.type}`}
              data-free={s.free}
            >
              <span aria-hidden="true">
                {t(`special.${s.type}`)}{' '}
                <span className={`font-semibold tabular-nums ${s.free > 0 ? 'text-ok' : ''}`}>
                  {s.free}
                </span>
                <span className="text-muted tabular-nums"> / {s.capacity}</span>
              </span>
              <span className="sr-only">
                {t('special.count', {
                  type: t(`special.${s.type}`),
                  free: s.free,
                  capacity: s.capacity,
                })}
              </span>
            </span>
          ))}
        </div>
      )}
      {zone.stale && (
        <p className="flex items-center gap-1.5 text-sm">
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            className="size-4 shrink-0"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
          >
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7v5l3 2" />
          </svg>
          {t('zone.stale')}
        </p>
      )}
      {isEstimated(zone.confidence) && (
        <p className="flex items-center gap-1.5 text-sm text-muted">
          <span aria-hidden="true">ⓘ</span>
          {t(zone.method === 'flow' ? 'zone.estimated_flow' : 'zone.estimated_camera')}
        </p>
      )}
      {zone.slots && <SlotMap zone={zone} />}
    </li>
  )
}
