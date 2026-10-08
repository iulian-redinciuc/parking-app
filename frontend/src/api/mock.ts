// Mock mode (`VITE_API_BASE=mock`, docs/design/frontend.md §3): a realistic, changing
// `LotStatus` for UI work and Playwright, behind the same `LiveFeed` interface as live.ts.
import lotInfoJson from './__fixtures__/lot-info.json'
import type { Level, LiveFeed, LiveState, LotInfo, LotStatus, Trend, ZoneStatus } from './types'

/**
 * Forced edge states for UI checks (`?mock=<scenario>` in mock mode, P3.5): `loading` never
 * answers, `offline` / `unreachable` freeze the last status with the connection down, `unavailable`
 * is a 503, `stale` freezes the first zone 5 min back, `estimated` lowers the last zone's confidence.
 */
export const MOCK_SCENARIOS = [
  'loading',
  'offline',
  'unreachable',
  'unavailable',
  'stale',
  'estimated',
] as const
export type MockScenario = (typeof MOCK_SCENARIOS)[number]

/** The `?mock=` scenario from a URL query string (`server_unreachable` is accepted too). */
export function mockScenarioFrom(search: string): MockScenario | undefined {
  let name = new URLSearchParams(search).get('mock')
  if (name === 'server_unreachable') name = 'unreachable'
  return MOCK_SCENARIOS.find((s) => s === name)
}

/** How old the frozen zone of the `stale` scenario is. */
export const STALE_SCENARIO_AGE_MS = 5 * 60_000
/** How long ago the `offline` / `unreachable` scenarios last heard from the server. */
export const DOWN_SCENARIO_AGE_MS = 60_000

export interface MockOptions {
  /** Seed for a repeatable feed (tests); random by default. */
  seed?: number
  /** A status every `minDelayMs`–`maxDelayMs` (3–8 s). */
  minDelayMs?: number
  maxDelayMs?: number
  lot?: LotInfo
  /** For tests; defaults to `Date.now`. */
  now?: () => number
  /** Force an edge state; turns the random episodes off. */
  scenario?: MockScenario
}

// chance per update that an episode starts, and how many updates it lasts
const P_STALE = 0.06
const P_LOW_CONFIDENCE = 0.08
const P_UNAVAILABLE = 0.02
const EPISODE = [2, 4] as const
const TREND_WINDOW = 5
// confidence when the camera is fine, per zone method (vision.md §8)
const BASE_CONFIDENCE = { slots: 1, count: 0.95, flow: 0.86 } as const

export function mockLotInfo(): LotInfo {
  return structuredClone(lotInfoJson as LotInfo)
}

/** mulberry32: small, fast, seedable. */
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Same rule as the server (`parking/core/fusion.py` `level_for`). */
export function levelFor(free: number, capacity: number, plenty = 0.2, filling = 0.05): Level {
  if (free <= 0 || capacity <= 0) return 'full'
  const ratio = free / capacity
  if (ratio >= plenty) return 'plenty'
  if (ratio >= filling) return 'filling'
  return 'almost_full'
}

/** Same rule as the server (`trend_for`): Δfree beyond ±max(2, 5 % of capacity). */
export function trendFor(deltaFree: number, capacity: number): Trend {
  const threshold = Math.max(2, 0.05 * capacity)
  if (deltaFree <= -threshold) return 'filling'
  if (deltaFree >= threshold) return 'emptying'
  return 'steady'
}

interface MockZone {
  info: LotInfo['zones'][number]
  occupied: number
  /** Cars per update the zone drifts by; itself a random walk, which makes trends. */
  pressure: number
  history: number[]
  slots: string[] | null
  taken: Set<string>
  staleFor: number
  lowConfidenceFor: number
  confidence: number
  updatedAt: string | null
}

/** A pure simulation: `step()` advances one update and returns the new status (or `null`). */
export function createMockLot(
  options: { seed?: number; lot?: LotInfo; now?: () => number; episodes?: boolean } = {},
) {
  const lot = options.lot ?? mockLotInfo()
  const random = seededRandom(options.seed ?? Math.floor(Math.random() * 2 ** 32))
  const now = options.now ?? Date.now
  const int = (lo: number, hi: number) => lo + Math.floor(random() * (hi - lo + 1))
  const episode = () => int(EPISODE[0], EPISODE[1])
  // with episodes off, every zone stays fresh and confident and the lot always answers
  const chance = (p: number) => options.episodes !== false && random() < p
  let unavailableFor = 0

  const zones: MockZone[] = lot.zones.map((info) => {
    const occupied = Math.round(info.capacity * (0.5 + random() * 0.4))
    const slots =
      info.method === 'slots'
        ? Array.from(
            { length: info.capacity },
            (_, i) => `${info.id[0].toUpperCase()}${String(i + 1).padStart(2, '0')}`,
          )
        : null
    return {
      info,
      occupied,
      pressure: 0,
      history: [info.capacity - occupied],
      slots,
      taken: new Set(slots?.slice(0, occupied)),
      staleFor: 0,
      lowConfidenceFor: 0,
      confidence: BASE_CONFIDENCE[info.method],
      updatedAt: null,
    }
  })

  function moveSlots(z: MockZone) {
    if (!z.slots) return
    const free = z.slots.filter((s) => !z.taken.has(s))
    while (z.taken.size < z.occupied) z.taken.add(free.splice(int(0, free.length - 1), 1)[0])
    const taken = [...z.taken]
    while (z.taken.size > z.occupied) z.taken.delete(taken.splice(int(0, taken.length - 1), 1)[0])
  }

  function stepZone(z: MockZone, ts: string) {
    const cap = z.info.capacity
    if (z.staleFor > 0) z.staleFor--
    else if (chance(P_STALE)) z.staleFor = episode()
    if (z.lowConfidenceFor > 0) z.lowConfidenceFor--
    else if (chance(P_LOW_CONFIDENCE)) z.lowConfidenceFor = episode()

    if (z.staleFor === 0) {
      z.pressure = Math.max(-2, Math.min(2, z.pressure + (random() - 0.5)))
      const move = Math.round(z.pressure + (random() - 0.5) * 2)
      z.occupied = Math.max(0, Math.min(cap, z.occupied + move))
      // bounce off the ends so the lot doesn't stick at full or empty
      if (z.occupied === cap || z.occupied === 0) z.pressure = -z.pressure
      z.confidence =
        z.lowConfidenceFor > 0
          ? Math.round((0.4 + random() * 0.3) * 100) / 100
          : BASE_CONFIDENCE[z.info.method]
      z.updatedAt = ts
      moveSlots(z)
    }
    z.history = [...z.history, cap - z.occupied].slice(-(TREND_WINDOW + 1))
  }

  function zoneStatus(z: MockZone): ZoneStatus {
    const cap = z.info.capacity
    const free = cap - z.occupied
    return {
      id: z.info.id,
      name: z.info.name,
      method: z.info.method,
      capacity: cap,
      occupied: z.occupied,
      free,
      level: levelFor(free, cap, lot.levels.plenty, lot.levels.filling),
      confidence: z.confidence,
      stale: z.staleFor > 0,
      trend: trendFor(free - z.history[0], cap),
      updated_at: z.updatedAt,
      slots: z.slots ? Object.fromEntries(z.slots.map((s) => [s, z.taken.has(s)])) : null,
    }
  }

  const status = (ts: string) => lotStatus(lot, zones.map(zoneStatus), ts)

  return {
    lot,
    /** Advance one update. `null` = the server would answer 503 `unavailable`. */
    step(): LotStatus | null {
      const ts = new Date(now()).toISOString()
      if (unavailableFor > 0) unavailableFor--
      else if (chance(P_UNAVAILABLE)) unavailableFor = episode()
      for (const z of zones) stepZone(z, ts)
      return unavailableFor > 0 ? null : status(ts)
    },
  }
}

/** A lot status with the totals computed from `zones`, as the server does. */
export function lotStatus(lot: LotInfo, zones: ZoneStatus[], ts: string): LotStatus {
  const capacity = zones.reduce((n, z) => n + z.capacity, 0)
  const free = zones.reduce((n, z) => n + z.free, 0)
  return {
    v: 1,
    lot: lot.id,
    updated_at: ts,
    total: {
      capacity,
      occupied: capacity - free,
      free,
      level: levelFor(free, capacity, lot.levels.plenty, lot.levels.filling),
      confidence: Math.min(...zones.map((z) => z.confidence)),
      stale: zones.some((z) => z.stale),
    },
    zones,
  }
}

/** One plausible status (mock-mode `getStatus()`). */
export function mockStatus(seed?: number): LotStatus {
  const sim = createMockLot({ seed })
  let status = sim.step()
  while (!status) status = sim.step()
  return status
}

/** A `LiveFeed` that publishes a new mock status every 3–8 s once started. */
export function createMockFeed(options: MockOptions = {}): LiveFeed {
  const { minDelayMs = 3000, maxDelayMs = 8000, now = Date.now, scenario } = options
  const sim = createMockLot({ ...options, episodes: scenario === undefined })
  const delays = seededRandom((options.seed ?? Math.floor(Math.random() * 2 ** 32)) ^ 0x5bd1e995)
  const listeners = new Set<() => void>()
  let state: LiveState = {
    status: null,
    connection: 'connecting',
    lastMessageAt: null,
    error: null,
  }
  let timer: ReturnType<typeof setTimeout> | null = null
  let started = false
  let frozenZone: ZoneStatus | null = null

  function set(next: LiveState) {
    state = next
    listeners.forEach((l) => l())
  }

  /** The scenario's twist on a fresh status. */
  function shape(status: LotStatus): LotStatus {
    const zones = [...status.zones]
    if (scenario === 'stale') {
      frozenZone ??= {
        ...zones[0],
        stale: true,
        updated_at: new Date(now() - STALE_SCENARIO_AGE_MS).toISOString(),
      }
      zones[0] = frozenZone
    } else if (scenario === 'estimated') {
      zones[zones.length - 1] = { ...zones[zones.length - 1], confidence: 0.6 }
    } else {
      return status
    }
    return lotStatus(sim.lot, zones, status.updated_at)
  }

  function tick() {
    // offline: no news reaches the app, as with the real feed
    if (offline) {
      timer = setTimeout(tick, minDelayMs)
      return
    }
    const status = sim.step()
    set(
      status
        ? { status: shape(status), connection: 'live', lastMessageAt: now(), error: null }
        : {
            status: null,
            connection: 'live',
            lastMessageAt: now(),
            error: {
              code: 'unavailable',
              message: 'no data received yet since start-up',
              status: 503,
            },
          },
    )
    timer = setTimeout(tick, minDelayMs + delays() * (maxDelayMs - minDelayMs))
  }

  /** The scenarios that never update. */
  function frozen(): LiveState | null {
    const lastMessageAt = now() - DOWN_SCENARIO_AGE_MS
    switch (scenario) {
      case 'loading':
        return state
      case 'offline':
        return { status: sim.step(), connection: 'offline', lastMessageAt, error: null }
      case 'unreachable':
        return {
          status: sim.step(),
          connection: 'error',
          lastMessageAt,
          error: { code: 'network', message: 'Failed to fetch' },
        }
      case 'unavailable':
        return {
          status: null,
          connection: 'live',
          lastMessageAt: now(),
          error: {
            code: 'unavailable',
            message: 'no data received yet since start-up',
            status: 503,
          },
        }
      default:
        return null
    }
  }

  // the browser's own offline/online, so a real loss of network looks like it does live
  let offline = false
  function onOffline() {
    offline = true
    set({ ...state, connection: 'offline' })
  }
  function onOnline() {
    offline = false
    set({ ...state, connection: state.status || state.error ? 'live' : 'connecting' })
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    start() {
      if (started) return
      started = true
      const fixed = frozen()
      if (fixed) {
        if (fixed !== state) set(fixed)
        return
      }
      window.addEventListener('offline', onOffline)
      window.addEventListener('online', onOnline)
      if (typeof navigator !== 'undefined' && navigator.onLine === false) onOffline()
      tick()
    },
    stop() {
      if (!started) return
      started = false
      window.removeEventListener('offline', onOffline)
      window.removeEventListener('online', onOnline)
      if (timer !== null) clearTimeout(timer)
      timer = null
    },
  }
}
