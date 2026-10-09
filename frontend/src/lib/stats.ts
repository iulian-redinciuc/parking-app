// Stats screen maths (frontend.md §2.3, P7.7): lot-local calendar parts from the lot's IANA
// timezone, the "typical" band (median + IQR of the same weekday's hours over 8 weeks), the
// weekday × hour heatmap and the downsampled "today" line. All pure, so they're unit-tested.
import type { HistoryPoint } from '../api/types'

export const HOUR_MS = 3600_000
export const DAY_MS = 24 * HOUR_MS
/** How many weeks the typical band and the heatmap look back (as `/api/forecast` does). */
export const TYPICAL_WEEKS = 8
/** Fewer same-weekday samples than this for an hour → no band there (the forecast's rule). */
export const MIN_TYPICAL_SAMPLES = 3
/** The today line averages its minute points into steps this long. */
export const TODAY_STEP_MIN = 15

export interface LocalParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
  /** 1 = Monday … 7 = Sunday. */
  weekday: number
}

const formatters = new Map<string, Intl.DateTimeFormat>()
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      weekday: 'short',
    })
    formatters.set(tz, f)
  }
  return f
}

/** The wall-clock parts of the instant `ms` in the timezone `tz`. */
export function lotParts(ms: number, tz: string): LocalParts {
  const parts: Record<string, string> = {}
  for (const p of formatter(tz).formatToParts(ms)) parts[p.type] = p.value
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    weekday: WEEKDAYS.indexOf(parts.weekday) + 1,
  }
}

/** `2026-10-09`: the lot-local date of `ms`, for comparing days. */
export function dateKey(ms: number, tz: string): string {
  const p = lotParts(ms, tz)
  return `${p.year}-${String(p.month).padStart(2, '0')}-${String(p.day).padStart(2, '0')}`
}

/** The UTC instant of the lot-local midnight starting the day that holds `ms`. */
export function startOfLotDay(ms: number, tz: string): number {
  const offset = (at: number) => {
    const p = lotParts(at, tz)
    return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) - Math.floor(at / 60_000) * 60_000
  }
  const p = lotParts(ms, tz)
  const wall = Date.UTC(p.year, p.month - 1, p.day)
  // the offset at midnight can differ from the one now (DST); a second pass settles it
  const guess = wall - offset(wall)
  return wall - offset(guess)
}

/** Hours since the lot-local midnight as wall-clock time (13:30 → 13.5), the charts' x axis. */
export function hourOfDay(ms: number, tz: string): number {
  const p = lotParts(ms, tz)
  return p.hour + p.minute / 60
}

/** Linear-interpolated quantile of sorted values (q in 0…1). */
export function quantile(sorted: number[], q: number): number {
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

export interface HourBand {
  /** Lot-local hour 0–23. */
  hour: number
  median: number
  q1: number
  q3: number
  samples: number
}

/**
 * The typical band for one weekday: for each lot-local hour, the median and the quartiles of the
 * hourly `free_avg` on that weekday (1 = Monday) among `points`, skipping the day `excludeDate`
 * (today) and hours with fewer than `MIN_TYPICAL_SAMPLES` values.
 */
export function typicalBand(
  points: HistoryPoint[],
  weekday: number,
  tz: string,
  excludeDate?: string,
): HourBand[] {
  const byHour: number[][] = Array.from({ length: 24 }, () => [])
  for (const point of points) {
    const ms = Date.parse(point.t)
    const p = lotParts(ms, tz)
    if (p.weekday !== weekday || dateKey(ms, tz) === excludeDate) continue
    byHour[p.hour].push(point.free_avg)
  }
  const bands: HourBand[] = []
  byHour.forEach((values, hour) => {
    if (values.length < MIN_TYPICAL_SAMPLES) return
    const sorted = [...values].sort((a, b) => a - b)
    bands.push({
      hour,
      median: quantile(sorted, 0.5),
      q1: quantile(sorted, 0.25),
      q3: quantile(sorted, 0.75),
      samples: sorted.length,
    })
  })
  return bands
}

/** Average free per lot-local weekday (row 0 = Monday) × hour (column 0–23); `null` = no data. */
export function weekHeatmap(
  points: HistoryPoint[],
  tz: string,
  excludeDate?: string,
): (number | null)[][] {
  const sums = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => [0, 0]))
  for (const point of points) {
    const ms = Date.parse(point.t)
    if (dateKey(ms, tz) === excludeDate) continue
    const p = lotParts(ms, tz)
    const cell = sums[p.weekday - 1][p.hour]
    cell[0] += point.free_avg
    cell[1] += 1
  }
  return sums.map((row) => row.map(([sum, n]) => (n ? sum / n : null)))
}

export interface TodayPoint {
  /** Lot-local hour of the step start (see `hourOfDay`). */
  x: number
  free: number
}

/**
 * Today's line: the minute points of the lot-local day `date`, averaged into `stepMin` steps
 * (fewer points to draw on a phone, the same shape).
 */
export function todaySeries(
  points: HistoryPoint[],
  tz: string,
  date: string,
  stepMin = TODAY_STEP_MIN,
): TodayPoint[] {
  const step = stepMin * 60_000
  const groups = new Map<number, [number, number]>()
  for (const point of points) {
    const ms = Date.parse(point.t)
    if (dateKey(ms, tz) !== date) continue
    const key = Math.floor(ms / step) * step
    const group = groups.get(key) ?? [0, 0]
    group[0] += point.free_avg
    group[1] += 1
    groups.set(key, group)
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([key, [sum, n]]) => ({ x: hourOfDay(key, tz), free: sum / n }))
}

/** Mean of today's points per lot-local hour, for the table fallback. */
export function hourlyMeans(series: TodayPoint[]): Map<number, number> {
  const sums = new Map<number, [number, number]>()
  for (const { x, free } of series) {
    const hour = Math.floor(x)
    const s = sums.get(hour) ?? [0, 0]
    sums.set(hour, [s[0] + free, s[1] + 1])
  }
  return new Map([...sums].map(([hour, [sum, n]]) => [hour, sum / n]))
}

const uiFormatters = new Map<string, Intl.DateTimeFormat>()

/** One cached UTC formatter per locale and options (making one costs far more than using it). */
function uiFormatter(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}|${JSON.stringify(options)}`
  let f = uiFormatters.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' })
    uiFormatters.set(key, f)
  }
  return f
}

/** A lot-local hour of day (13.5) as a clock time in the UI language ("13:30", "1:30 PM"). */
export function formatClock(x: number, locale: string): string {
  const minutes = Math.round(x * 60)
  return uiFormatter(locale, { hour: 'numeric', minute: '2-digit' }).format(
    Date.UTC(2024, 0, 1, Math.floor(minutes / 60), minutes % 60),
  )
}

/** "Mon", "Tue"… (1 = Monday) in the UI language; `long` for "Monday". */
export function weekdayName(weekday: number, locale: string, long = false): string {
  // 2024-01-01 was a Monday
  return uiFormatter(locale, { weekday: long ? 'long' : 'short' }).format(
    Date.UTC(2024, 0, weekday),
  )
}
