import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  MOCK_SCENARIOS,
  type MockScenario,
  correctMockZone,
  createMockFeed,
  createMockLot,
  levelFor,
  mockScenarioFrom,
  mockSlotMap,
  mockStatus,
  trendFor,
} from './mock'
import { parseSlotMap } from '../lib/slotMap'
import type { LotStatus } from './types'
import { isLotStatus } from './validate'

function run(steps: number, seed = 1) {
  const sim = createMockLot({ seed, now: () => Date.UTC(2026, 9, 9, 8) })
  return Array.from({ length: steps }, () => sim.step())
}

describe('mock feed', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('uses the server rules for level and trend', () => {
    expect(levelFor(23, 100)).toBe('plenty')
    expect(levelFor(12, 100)).toBe('filling')
    expect(levelFor(1, 100)).toBe('almost_full')
    expect(levelFor(0, 100)).toBe('full')
    expect(levelFor(5, 0)).toBe('full')
    expect(trendFor(-3, 60)).toBe('filling')
    expect(trendFor(3, 60)).toBe('emptying')
    expect(trendFor(2, 60)).toBe('steady')
  })

  it('is repeatable with a seed', () => {
    expect(run(20, 7)).toEqual(run(20, 7))
    expect(run(20, 7)).not.toEqual(run(20, 8))
  })

  it('produces valid, consistent statuses with every edge state', () => {
    const results = run(500)
    const statuses = results.filter((s): s is LotStatus => s !== null)
    expect(statuses.length).toBeGreaterThan(400)
    for (const s of statuses) {
      expect(isLotStatus(s)).toBe(true)
      expect(s.total.free).toBe(s.zones.reduce((n, z) => n + z.free, 0))
      expect(s.total.confidence).toBe(Math.min(...s.zones.map((z) => z.confidence)))
      expect(s.total.stale).toBe(s.zones.some((z) => z.stale))
      for (const z of s.zones) {
        expect(z.free).toBe(z.capacity - z.occupied)
        expect(z.level).toBe(levelFor(z.free, z.capacity))
        if (z.slots) expect(Object.values(z.slots).filter(Boolean)).toHaveLength(z.occupied)
        else expect(z.method).not.toBe('slots')
      }
    }
    const zones = statuses.flatMap((s) => s.zones)
    // it walks: counts change, trends appear, and edge states show up sometimes, not always
    expect(new Set(statuses.map((s) => s.total.free)).size).toBeGreaterThan(5)
    expect(new Set(zones.map((z) => z.trend))).toEqual(new Set(['filling', 'emptying', 'steady']))
    const share = (n: number) => n / zones.length
    expect(share(zones.filter((z) => z.stale).length)).toBeGreaterThan(0.05)
    expect(share(zones.filter((z) => z.stale).length)).toBeLessThan(0.4)
    expect(share(zones.filter((z) => z.confidence < 0.8).length)).toBeGreaterThan(0.05)
    expect(share(zones.filter((z) => z.confidence < 0.8).length)).toBeLessThan(0.5)
    expect(results).toContain(null)
  })

  it('mockStatus() always returns a status', () => {
    for (let seed = 0; seed < 50; seed++) expect(isLotStatus(mockStatus(seed))).toBe(true)
  })

  it('publishes through the LiveFeed interface every 3–8 s', () => {
    vi.useFakeTimers()
    const feed = createMockFeed({ seed: 3 })
    const listener = vi.fn()
    const unsubscribe = feed.subscribe(listener)
    expect(feed.getSnapshot()).toMatchObject({ status: null, connection: 'connecting' })

    feed.start()
    expect(listener).toHaveBeenCalledTimes(1)
    const first = feed.getSnapshot()
    expect(first.connection).toBe('live')
    expect(first.lastMessageAt).toBe(Date.now())
    feed.start() // already running: no second timer
    vi.advanceTimersByTime(2_999)
    expect(listener).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(5_001)
    expect(listener).toHaveBeenCalledTimes(2)
    expect(feed.getSnapshot()).not.toBe(first)

    vi.advanceTimersByTime(80_000)
    const calls = listener.mock.calls.length
    expect(calls).toBeGreaterThanOrEqual(2 + 10)
    expect(calls).toBeLessThanOrEqual(2 + 27)

    feed.stop()
    vi.advanceTimersByTime(60_000)
    expect(listener).toHaveBeenCalledTimes(calls)
    unsubscribe()
    feed.start()
    expect(listener).toHaveBeenCalledTimes(calls)
    feed.stop()
  })

  it('a correction reaches running feeds at once, clamped, with full confidence', () => {
    vi.useFakeTimers()
    const feed = createMockFeed({ seed: 5, scenario: 'estimated' })
    const idle = createMockFeed({ seed: 6 })
    feed.start()
    const listener = vi.fn()
    feed.subscribe(listener)
    const before = feed.getSnapshot().status!.zones.find((z) => z.id === 'underground')!
    const corrected = correctMockZone('underground', 70)
    expect(listener).toHaveBeenCalledTimes(1)
    const zone = feed.getSnapshot().status!.zones.find((z) => z.id === 'underground')!
    expect(zone).toMatchObject({ occupied: 60, free: 0, level: 'full' })
    expect(corrected!.old).toBe(before.occupied)
    expect(corrected!.status.zones.find((z) => z.id === 'underground')!.occupied).toBe(60)
    expect(idle.getSnapshot().status).toBeNull() // not started: not corrected
    feed.stop()
    expect(correctMockZone('underground', 3)).toBeNull()
  })

  it('reports `unavailable` like a 503 from the server', () => {
    vi.useFakeTimers()
    const feed = createMockFeed({ seed: 1, minDelayMs: 10, maxDelayMs: 10 })
    feed.start()
    let snapshot = feed.getSnapshot()
    for (let i = 0; i < 500 && snapshot.status; i++) {
      vi.advanceTimersByTime(10)
      snapshot = feed.getSnapshot()
    }
    feed.stop()
    expect(snapshot).toMatchObject({
      status: null,
      connection: 'live',
      error: { code: 'unavailable' },
    })
  })
})

describe('mock scenarios (?mock=…)', () => {
  afterEach(() => vi.useRealTimers())

  it('reads the scenario from the query string', () => {
    expect(mockScenarioFrom('?mock=stale')).toBe('stale')
    expect(mockScenarioFrom('?mock=server_unreachable')).toBe('unreachable')
    expect(mockScenarioFrom('?mock=nope')).toBeUndefined()
    expect(mockScenarioFrom('')).toBeUndefined()
    for (const s of MOCK_SCENARIOS) expect(mockScenarioFrom(`?x=1&mock=${s}`)).toBe(s)
  })

  it('freezes the connection-down and loading states', () => {
    vi.useFakeTimers()
    let now = 0
    const snapshot = (scenario: MockScenario) => {
      now = Date.now()
      const feed = createMockFeed({ seed: 1, scenario })
      feed.start()
      const first = feed.getSnapshot()
      vi.advanceTimersByTime(60_000)
      expect(feed.getSnapshot()).toBe(first) // never updates
      feed.stop()
      return first
    }
    expect(snapshot('loading')).toMatchObject({ status: null, connection: 'connecting' })
    const offline = snapshot('offline')
    expect(offline).toMatchObject({ connection: 'offline', lastMessageAt: now - 60_000 })
    expect(isLotStatus(offline.status)).toBe(true)
    expect(snapshot('unreachable')).toMatchObject({
      connection: 'error',
      error: { code: 'network' },
      lastMessageAt: now - 60_000,
    })
    expect(snapshot('unavailable')).toMatchObject({
      status: null,
      connection: 'live',
      error: { code: 'unavailable' },
    })
  })

  it('stale keeps the first zone frozen and 5 min old; estimated lowers the last zone', () => {
    vi.useFakeTimers()
    const start = Date.now()
    const feed = createMockFeed({ seed: 2, scenario: 'stale', minDelayMs: 10, maxDelayMs: 10 })
    feed.start()
    const first = feed.getSnapshot().status!
    for (let i = 0; i < 30; i++) {
      vi.advanceTimersByTime(10)
      const s = feed.getSnapshot().status!
      expect(isLotStatus(s)).toBe(true)
      expect(s.zones[0]).toEqual(first.zones[0])
      expect(s.zones[0]).toMatchObject({ stale: true })
      expect(Date.parse(s.zones[0].updated_at!)).toBe(start - 5 * 60_000)
      expect(s.zones[1].stale).toBe(false)
      expect(s.total.stale).toBe(true)
      expect(s.total.free).toBe(s.zones[0].free + s.zones[1].free)
    }
    feed.stop()

    const est = createMockFeed({ seed: 2, scenario: 'estimated', minDelayMs: 10, maxDelayMs: 10 })
    est.start()
    for (let i = 0; i < 30; i++) {
      vi.advanceTimersByTime(10)
      const s = est.getSnapshot().status!
      expect(s.zones.map((z) => z.confidence)).toEqual([1, 0.6])
      expect(s.total).toMatchObject({ confidence: 0.6, stale: false })
    }
    est.stop()
  })

  it('follows the browser going offline and back online', () => {
    vi.useFakeTimers()
    const feed = createMockFeed({ seed: 4 })
    feed.start()
    window.dispatchEvent(new Event('offline'))
    expect(feed.getSnapshot().connection).toBe('offline')
    expect(feed.getSnapshot().status).not.toBeNull()
    vi.advanceTimersByTime(20_000) // no news while offline
    expect(feed.getSnapshot().connection).toBe('offline')
    window.dispatchEvent(new Event('online'))
    expect(feed.getSnapshot().connection).toBe('live')
    feed.stop()
    window.dispatchEvent(new Event('offline'))
    expect(feed.getSnapshot().connection).toBe('live')
  })

  it('starts offline when the browser already is (an app opened with no network)', () => {
    vi.useFakeTimers()
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    const feed = createMockFeed({ seed: 4 })
    feed.start()
    expect(feed.getSnapshot().connection).toBe('offline')
    vi.advanceTimersByTime(20_000)
    expect(feed.getSnapshot().connection).toBe('offline')
    onLine.mockRestore()
    window.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(10_000)
    expect(feed.getSnapshot().connection).toBe('live')
    expect(feed.getSnapshot().status).not.toBeNull()
    feed.stop()
  })
})

describe('mock slot map', () => {
  it('has one shape per slot of the mock status, and none for a zone without slots', () => {
    const ground = mockStatus().zones.find((z) => z.id === 'ground')!
    const map = parseSlotMap(mockSlotMap('ground')!)!
    expect([...map.ids].sort()).toEqual(Object.keys(ground.slots!).sort())
    expect(map.nodes.some((n) => n.tag === 'line')).toBe(true)
    expect(mockSlotMap('underground')).toBeNull()
    expect(mockSlotMap('nowhere')).toBeNull()
  })
})
