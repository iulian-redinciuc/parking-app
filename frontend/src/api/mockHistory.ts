// Mock `/api/history` and `/api/forecast` (P7.7) for the mock build: a repeatable weekly curve per
// zone in the lot's timezone (busy on workday office hours, quieter at weekends) with a little
// noise per bucket. Loaded lazily by client.ts, so none of this is in the main chunk.
import { lotParts, startOfLotDay } from '../lib/stats'
import { ApiRequestError, type HistoryQuery } from './client'
import { mockLotInfo } from './mock'
import type { Forecast, History, HistoryPoint, LotZoneInfo } from './types'

const STEP_MS = { minute: 60_000, hour: 3600_000 } as const
const MAX_POINTS = 2000

/** Occupied share 0…1 at a lot-local weekday (1 = Monday) and hour (fractional). */
function busyShare(weekday: number, hour: number): number {
  const workday = weekday <= 5
  const peak = workday ? 0.85 : 0.45
  const centre = workday ? 12.5 : 14
  const width = workday ? 4 : 3.5
  return 0.08 + peak * Math.exp(-(((hour - centre) / width) ** 2))
}

/** Deterministic noise in −1…1 from a bucket start and a zone. */
function noise(ms: number, zone: string): number {
  let h = Math.floor(ms / 60_000) ^ (zone.length * 2654435761)
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b)
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b)
  return (((h ^ (h >>> 16)) >>> 0) / 0xffffffff) * 2 - 1
}

function zoneFree(zone: LotZoneInfo, ms: number, tz: string, jitter = true): number {
  const p = lotParts(ms, tz)
  const share = busyShare(p.weekday, p.hour + p.minute / 60)
  const free =
    zone.capacity * (1 - share) + (jitter ? noise(ms, zone.id) * 0.06 * zone.capacity : 0)
  return Math.min(zone.capacity, Math.max(0, free))
}

function zonesFor(id: string): LotZoneInfo[] {
  const zones = mockLotInfo().zones
  if (id === 'total') return zones
  const zone = zones.find((z) => z.id === id)
  if (!zone)
    throw new ApiRequestError({ code: 'not_found', message: `unknown zone ${id}`, status: 404 })
  return [zone]
}

const round1 = (x: number) => Math.round(x * 10) / 10

export function mockHistory(
  { zone = 'total', from, to, bucket = 'hour' }: HistoryQuery,
  now = Date.now(),
): History {
  const zones = zonesFor(zone)
  const tz = mockLotInfo().timezone
  const capacity = zones.reduce((sum, z) => sum + z.capacity, 0)
  const end = to ? Date.parse(to) : now
  const start = from ? Date.parse(from) : end - (bucket === 'day' ? 30 : 1) * 86_400_000
  const step = bucket === 'day' ? 86_400_000 : STEP_MS[bucket]
  if (start >= end || (end - start) / step > MAX_POINTS) {
    throw new ApiRequestError({ code: 'bad_request', message: 'bad range', status: 422 })
  }
  const points: HistoryPoint[] = []
  let t = bucket === 'day' ? startOfLotDay(start, tz) : Math.floor(start / step) * step
  while (t < Math.min(end, now)) {
    const next = bucket === 'day' ? startOfLotDay(t + 30 * 3600_000, tz) : t + step
    const free = zones.reduce((sum, z) => sum + zoneFree(z, t + (next - t) / 2, tz), 0)
    const spread = bucket === 'minute' ? 0 : 0.1 * capacity
    points.push({
      t: new Date(t).toISOString(),
      free_avg: round1(free),
      free_min: Math.max(0, Math.round(free - spread)),
      free_max: Math.min(capacity, Math.round(free + spread)),
      occupied_avg: round1(capacity - free),
    })
    t = next
  }
  return {
    zone,
    bucket,
    from: new Date(start).toISOString(),
    to: new Date(end).toISOString(),
    points,
  }
}

export function mockForecast(
  { zone = 'total', at }: { zone?: string; at?: string },
  now = Date.now(),
): Forecast {
  const zones = zonesFor(zone)
  const tz = mockLotInfo().timezone
  const when = at ? Date.parse(at) : now + 30 * 60_000
  const free = zones.reduce((sum, z) => sum + zoneFree(z, when, tz, false), 0)
  return {
    zone,
    at: new Date(when).toISOString(),
    free_expected: Math.round(free),
    basis: 'median of last 8 same weekday/hour',
    samples: 8,
  }
}
