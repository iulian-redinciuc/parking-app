// How a status is shown: level → word + colour (api.md "Levels"), trend → arrow + words, and the
// "≈" prefix for estimated numbers (vision.md §8). Words are i18n keys, translated when asked for.
import type { Level, SpaceType, Trend, ZoneStatus } from '../api/types'
import { t } from '../i18n'

export type Tone = 'ok' | 'warn' | 'bad'

export interface LevelInfo {
  key: string
  /** The word in the current language. */
  label: string
  tone: Tone
}

export const LEVELS: Record<Level, { key: string; tone: Tone }> = {
  plenty: { key: 'level.plenty', tone: 'ok' },
  filling: { key: 'level.filling', tone: 'warn' },
  almost_full: { key: 'level.almost_full', tone: 'bad' },
  full: { key: 'level.full', tone: 'bad' },
}

// Full class names, so Tailwind finds them.
export const TONE_TEXT: Record<Tone, string> = { ok: 'text-ok', warn: 'text-warn', bad: 'text-bad' }
export const TONE_BG: Record<Tone, string> = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-bad' }

export function levelInfo(level: Level): LevelInfo {
  const { key, tone } = LEVELS[level]
  return { key, label: t(key), tone }
}

export const TRENDS: Record<Trend, { key: string }> = {
  filling: { key: 'trend.filling' },
  emptying: { key: 'trend.emptying' },
  steady: { key: 'trend.steady' },
}

/** Below this confidence a number is an estimate: shown with "≈" and an "Estimated" note. */
export const ESTIMATED_BELOW = 0.8

export function isEstimated(confidence: number): boolean {
  return confidence < ESTIMATED_BELOW
}

/** The free count as shown: "12", or "≈ 11" when the confidence is below 0.8. */
export function formatFree({ free, confidence }: { free: number; confidence: number }): string {
  return isEstimated(confidence) ? `≈ ${free}` : String(free)
}

/** Share of the capacity that is taken (0–1), for the bars. */
export function occupiedShare({ capacity, occupied }: { capacity: number; occupied: number }) {
  return capacity > 0 ? Math.min(1, Math.max(0, occupied / capacity)) : 0
}

/** The special space types, in the order they are shown (the server's `SPACE_TYPES`). */
export const SPACE_TYPES: readonly SpaceType[] = ['accessible', 'ev', 'motorcycle', 'reserved']

export interface SpecialCount {
  type: SpaceType
  capacity: number
  free: number
}

/** A zone's special spaces (`by_type`) in display order; types this app doesn't know are skipped. */
export function specialSpaces(zone: Pick<ZoneStatus, 'by_type'>): SpecialCount[] {
  const counts = zone.by_type ?? {}
  return SPACE_TYPES.flatMap((type) => {
    const count = counts[type]
    return count ? [{ type, capacity: count.capacity, free: count.free }] : []
  })
}

/** The special spaces of several zones added up, for the types any of them has. */
export function specialTotals(zones: Pick<ZoneStatus, 'by_type'>[]): SpecialCount[] {
  const all = zones.flatMap(specialSpaces)
  return SPACE_TYPES.flatMap((type) => {
    const mine = all.filter((c) => c.type === type)
    if (mine.length === 0) return []
    const sum = (key: 'capacity' | 'free') => mine.reduce((n, c) => n + c[key], 0)
    return [{ type, capacity: sum('capacity'), free: sum('free') }]
  })
}
