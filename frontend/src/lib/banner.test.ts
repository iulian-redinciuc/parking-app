import { describe, expect, it } from 'vitest'
import staleJson from '../api/__fixtures__/lot-status-stale.json'
import statusJson from '../api/__fixtures__/lot-status.json'
import type { LiveState, LotStatus } from '../api/types'
import { UNREACHABLE_AFTER_MS, liveView, staleMinutes } from './banner'

const status = statusJson as LotStatus
const stale = staleJson as LotStatus
const T = Date.parse('2026-10-07T17:20:00Z')
const live = (patch: Partial<LiveState>): LiveState => ({
  status: null,
  connection: 'connecting',
  lastMessageAt: null,
  error: null,
  ...patch,
})
const network = { code: 'network', message: 'Failed to fetch' } as const
const unavailable = { code: 'unavailable', message: 'no data', status: 503 } as const

describe('liveView', () => {
  it('is loading until the first answer', () => {
    expect(liveView(live({}), T, T)).toEqual({
      banner: null,
      loading: true,
      status: null,
      dimAll: false,
    })
  })

  it('shows a fresh status with no banner', () => {
    const view = liveView(live({ status, connection: 'live', lastMessageAt: T }), T, T)
    expect(view).toEqual({ banner: null, loading: false, status, dimAll: false })
  })

  it('offline: banner, last known numbers greyed, or none at all', () => {
    const view = liveView(live({ status, connection: 'offline', lastMessageAt: T }), T, T)
    expect(view.banner).toMatchObject({ kind: 'offline', title: "You're offline" })
    expect(view).toMatchObject({ status, dimAll: true, loading: false })
    const empty = liveView(live({ connection: 'offline' }), T, T)
    expect(empty).toMatchObject({ banner: { kind: 'offline' }, loading: false, status: null })
  })

  it('server_unreachable only after failing for more than 20 s', () => {
    const failing = live({ status, connection: 'error', error: network, lastMessageAt: T })
    expect(liveView(failing, T + UNREACHABLE_AFTER_MS, T).banner).toBeNull()
    expect(liveView(failing, T + UNREACHABLE_AFTER_MS, T).dimAll).toBe(false)
    const view = liveView(failing, T + UNREACHABLE_AFTER_MS + 1, T)
    expect(view.banner).toMatchObject({
      kind: 'server_unreachable',
      title: "Can't reach the parking server",
    })
    expect(view).toMatchObject({ status, dimAll: true })
  })

  it('server_unreachable before any answer counts from the app start', () => {
    const failing = live({ connection: 'error', error: network })
    expect(liveView(failing, T + 10_000, T)).toMatchObject({ banner: null, loading: true })
    expect(liveView(failing, T + 21_000, T)).toMatchObject({
      banner: { kind: 'server_unreachable' },
      loading: false,
    })
  })

  it('a live stream with a failed poll is not unreachable', () => {
    const view = liveView(live({ status, connection: 'live', error: network }), T + 60_000, 0)
    expect(view.banner).toBeNull()
  })

  it('unavailable (503): banner, numbers hidden', () => {
    const view = liveView(live({ connection: 'live', error: unavailable, lastMessageAt: T }), T, T)
    expect(view).toMatchObject({
      banner: { kind: 'unavailable', title: 'Waiting for the first camera reading' },
      loading: false,
      status: null,
    })
  })

  it('stale: "Camera data is X min old", numbers shown', () => {
    const view = liveView(live({ status: stale, connection: 'live', lastMessageAt: T }), T, T)
    expect(view.banner).toMatchObject({
      kind: 'stale',
      title: 'Camera data is 7 min old',
      detail: 'Affected: Parter, Subsol',
      tone: 'warn',
    })
    expect(view).toMatchObject({ status: stale, dimAll: false })
  })

  it('priority: offline > server_unreachable > unavailable > stale', () => {
    const late = T + 60_000
    const base = { status: stale, lastMessageAt: T, error: network }
    expect(liveView(live({ ...base, connection: 'offline' }), late, T).banner?.kind).toBe('offline')
    expect(liveView(live({ ...base, connection: 'error' }), late, T).banner?.kind).toBe(
      'server_unreachable',
    )
    expect(
      liveView(live({ connection: 'polling', error: unavailable, lastMessageAt: T }), late, T)
        .banner?.kind,
    ).toBe('unavailable')
    expect(
      liveView(live({ status: stale, connection: 'polling', lastMessageAt: T }), late, T).banner
        ?.kind,
    ).toBe('stale')
  })
})

describe('staleMinutes', () => {
  it('is the oldest stale zone age in whole minutes, at least 1', () => {
    expect(staleMinutes(stale, T)).toBe(7)
    expect(staleMinutes(stale, Date.parse('2026-10-07T17:12:50Z'))).toBe(1)
  })

  it('is null when no stale zone has ever had data', () => {
    const never = { ...stale, zones: stale.zones.filter((z) => z.updated_at === null) }
    expect(staleMinutes(never, T)).toBeNull()
    expect(liveView(live({ status: never, connection: 'live' }), T, T).banner).toMatchObject({
      title: 'No camera data yet',
    })
  })
})
