// Relative times for the UI (strings become i18n keys in P3.6).

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', Infinity],
]

/** "Updated 3 seconds ago" for a `Date.now()` time `at`, as of `now`. */
export function formatUpdatedAgo(at: number, now: number, locale = 'en'): string {
  let amount = Math.floor(Math.max(0, now - at) / 1000)
  if (amount < 1) return 'Updated just now'
  const format = new Intl.RelativeTimeFormat(locale, { numeric: 'always' })
  for (const [unit, perNext] of UNITS) {
    if (amount < perNext) return `Updated ${format.format(-amount, unit)}`
    amount = Math.floor(amount / perNext)
  }
  return '' // unreachable: days never roll over
}
