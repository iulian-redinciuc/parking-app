// How a status is shown: level → word + colour (api.md "Levels"), trend → arrow + words, and the
// "≈" prefix for estimated numbers (vision.md §8). The `key`s are the i18n keys for P3.6; until
// then `label` is the English text.
import type { Level, Trend } from '../api/types'

export type Tone = 'ok' | 'warn' | 'bad'

export interface LevelInfo {
  key: string
  label: string
  tone: Tone
}

export const LEVELS: Record<Level, LevelInfo> = {
  plenty: { key: 'level.plenty', label: 'Plenty of space', tone: 'ok' },
  filling: { key: 'level.filling', label: 'Filling up', tone: 'warn' },
  almost_full: { key: 'level.almost_full', label: 'Almost full', tone: 'bad' },
  full: { key: 'level.full', label: 'Full', tone: 'bad' },
}

// Full class names, so Tailwind finds them.
export const TONE_TEXT: Record<Tone, string> = { ok: 'text-ok', warn: 'text-warn', bad: 'text-bad' }
export const TONE_BG: Record<Tone, string> = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-bad' }

export function levelInfo(level: Level): LevelInfo {
  return LEVELS[level]
}

export const TRENDS: Record<Trend, { key: string; label: string }> = {
  filling: { key: 'trend.filling', label: 'Getting fuller' },
  emptying: { key: 'trend.emptying', label: 'Emptying' },
  steady: { key: 'trend.steady', label: 'Steady' },
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
