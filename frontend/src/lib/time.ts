// Relative times for the UI: the words around the time are i18n keys; the time itself comes from
// `Intl.RelativeTimeFormat` in the UI language (it knows each language's plurals).
import i18n, { t } from '../i18n'

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', Infinity],
]

/** "Updated 3 seconds ago" for a `Date.now()` time `at`, as of `now`. */
export function formatUpdatedAgo(at: number, now: number, locale = i18n.language): string {
  let amount = Math.floor(Math.max(0, now - at) / 1000)
  if (amount < 1) return t('updated.just_now')
  const format = new Intl.RelativeTimeFormat(locale, { numeric: 'always' })
  for (const [unit, perNext] of UNITS) {
    if (amount < perNext) return t('updated.ago', { ago: format.format(-amount, unit) })
    amount = Math.floor(amount / perNext)
  }
  return '' // unreachable: days never roll over
}
