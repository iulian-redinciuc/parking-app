import type { ZoneStatus } from '../api/types'
import { TONE_TEXT, formatFree, isEstimated, levelInfo } from '../lib/status'
import LevelBar from './LevelBar'
import TrendIcon from './TrendIcon'

// One zone: name, free / capacity, trend, a bar with the level word, and a note when estimated.
// `dimmed` greys the numbers: they're old (stale zone, or the connection is down).
export default function ZoneCard({ zone, dimmed = false }: { zone: ZoneStatus; dimmed?: boolean }) {
  const level = levelInfo(zone.level)
  const nameId = `zone-${zone.id}`
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
            <span className="sr-only"> free</span>
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
          No fresh camera data: last known numbers
        </p>
      )}
      {isEstimated(zone.confidence) && (
        <p className="flex items-center gap-1.5 text-sm text-muted">
          <span aria-hidden="true">ⓘ</span>
          {zone.method === 'flow'
            ? 'Estimated from entry/exit counts'
            : 'Estimated: the camera view is unclear'}
        </p>
      )}
    </li>
  )
}
