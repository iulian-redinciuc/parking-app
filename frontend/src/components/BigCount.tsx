import type { Totals } from '../api/types'
import { useThrottled } from '../hooks/useThrottled'
import { TONE_TEXT, formatFree, levelInfo } from '../lib/status'
import LevelBar from './LevelBar'

/** Screen readers hear the count at most this often. */
export const ANNOUNCE_EVERY_MS = 30_000

// The lot's free count, the level word and a bar. The visible numbers change at once; the
// aria-live copy is throttled so a screen reader isn't interrupted by every update.
export default function BigCount({ total }: { total: Totals }) {
  const level = levelInfo(total.level)
  const count = formatFree(total)
  const unit = total.free === 1 ? 'free space' : 'free spaces'
  const announced = useThrottled(`${count} ${unit}, ${level.label}`, ANNOUNCE_EVERY_MS)
  return (
    <section className="flex flex-col items-center gap-1 py-8">
      <div aria-hidden="true" className="flex flex-col items-center">
        <span className="text-[5rem] leading-none font-bold tabular-nums" data-testid="big-count">
          {count}
        </span>
        <span className="text-lg text-muted">{unit}</span>
      </div>
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {announced}
      </p>
      <div className="mt-4 flex w-full max-w-xs items-center gap-3">
        <LevelBar level={total.level} capacity={total.capacity} occupied={total.occupied} />
        <span aria-hidden="true" className={`shrink-0 font-semibold ${TONE_TEXT[level.tone]}`}>
          {level.label}
        </span>
      </div>
    </section>
  )
}
