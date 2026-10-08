import type { Level } from '../api/types'
import { TONE_BG, levelInfo, occupiedShare } from '../lib/status'

// The taken share of a zone or the lot, coloured by level. Decorative: the numbers and the level
// word next to it say the same thing.
export default function LevelBar({
  level,
  capacity,
  occupied,
}: {
  level: Level
  capacity: number
  occupied: number
}) {
  const percent = Math.round(occupiedShare({ capacity, occupied }) * 100)
  return (
    <div
      aria-hidden="true"
      className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted/25"
    >
      <div
        className={`h-full rounded-full transition-[width] duration-500 ${TONE_BG[levelInfo(level).tone]}`}
        style={{ width: `${percent}%` }}
        data-percent={percent}
      />
    </div>
  )
}
