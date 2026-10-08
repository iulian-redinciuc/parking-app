import type { Trend } from '../api/types'
import { TRENDS } from '../lib/status'

// Free spaces going down (filling) ↘, up (emptying) ↗, or steady →; the words are for screen readers.
const ROTATION: Record<Trend, string> = { filling: 'rotate-45', emptying: '-rotate-45', steady: '' }

export default function TrendIcon({ trend }: { trend: Trend }) {
  const { label } = TRENDS[trend]
  return (
    <span className="inline-flex shrink-0 text-muted" title={label}>
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        className={`size-5 ${ROTATION[trend]}`}
        fill="none"
        stroke="currentColor"
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M4 12h15M13 6l6 6-6 6" />
      </svg>
      <span className="sr-only">Trend: {label.toLowerCase()}</span>
    </span>
  )
}
