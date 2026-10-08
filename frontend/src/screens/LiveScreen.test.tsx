import { act, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import staleJson from '../api/__fixtures__/lot-status-stale.json'
import statusJson from '../api/__fixtures__/lot-status.json'
import type { LiveFeed, LiveState, LotStatus } from '../api/types'
import LiveScreen from './LiveScreen'

let state: LiveState
const listeners = new Set<() => void>()
const fakeFeed: LiveFeed = {
  getSnapshot: () => state,
  subscribe: (l) => (listeners.add(l), () => listeners.delete(l)),
  start: () => {},
  stop: () => {},
}
vi.mock('../api/live', () => ({ liveFeed: () => fakeFeed }))

function push(next: Partial<LiveState>) {
  act(() => {
    state = { ...state, ...next }
    listeners.forEach((l) => l())
  })
}

describe('LiveScreen', () => {
  it('waits for data, then shows the total, one card per zone and the update time', () => {
    state = { status: null, connection: 'connecting', lastMessageAt: null, error: null }
    render(<LiveScreen />)
    expect(screen.getByRole('heading', { name: 'Live', level: 1 })).toBeInTheDocument()
    expect(screen.getByTestId('live-skeleton')).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByTestId('big-count')).not.toBeInTheDocument()

    push({ status: statusJson as LotStatus, connection: 'live', lastMessageAt: Date.now() })
    expect(screen.getByTestId('big-count')).toHaveTextContent('23')
    const zones = within(screen.getByRole('list', { name: 'Zones' })).getAllByRole('listitem')
    expect(zones.map((z) => z.getAttribute('aria-labelledby'))).toEqual([
      'zone-ground',
      'zone-underground',
    ])
    expect(screen.getByText(/^Updated/)).toBeInTheDocument()
    expect(screen.queryByTestId('live-skeleton')).not.toBeInTheDocument()
    expect(screen.queryByTestId('status-banner')).not.toBeInTheDocument()
  })

  it('offline: banner and the last known numbers greyed, not hidden', () => {
    state = { status: null, connection: 'connecting', lastMessageAt: null, error: null }
    render(<LiveScreen />)
    push({ status: statusJson as LotStatus, connection: 'offline', lastMessageAt: Date.now() })
    expect(screen.getByTestId('status-banner')).toHaveTextContent("You're offline")
    expect(screen.getByTestId('big-count').closest('section')).toHaveAttribute('data-dimmed')
    expect(screen.getByTestId('zone-ground')).toHaveClass('dimmed')
    expect(screen.getByTestId('zone-underground')).toHaveClass('dimmed')
  })

  it('unavailable: banner, no numbers, no skeleton', () => {
    state = {
      status: null,
      connection: 'live',
      lastMessageAt: Date.now(),
      error: { code: 'unavailable', message: 'no data', status: 503 },
    }
    render(<LiveScreen />)
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for the first camera reading')
    expect(screen.queryByTestId('big-count')).not.toBeInTheDocument()
    expect(screen.queryByTestId('live-skeleton')).not.toBeInTheDocument()
  })

  it('server unreachable after 20 s of failures', () => {
    vi.useFakeTimers()
    try {
      state = {
        status: statusJson as LotStatus,
        connection: 'error',
        lastMessageAt: Date.now(),
        error: { code: 'network', message: 'Failed to fetch' },
      }
      render(<LiveScreen />)
      expect(screen.queryByTestId('status-banner')).not.toBeInTheDocument()
      act(() => void vi.advanceTimersByTime(21_000))
      expect(screen.getByTestId('status-banner')).toHaveTextContent(
        "Can't reach the parking server",
      )
      expect(screen.getByTestId('zone-ground')).toHaveClass('dimmed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stale: banner with the age, only the stale zones greyed', () => {
    const fresh = { ...(staleJson as LotStatus).zones[1], stale: false, updated_at: null }
    const status = { ...(staleJson as LotStatus) }
    status.zones = [status.zones[0], fresh]
    state = { status, connection: 'live', lastMessageAt: Date.now(), error: null }
    render(<LiveScreen />)
    expect(screen.getByTestId('status-banner')).toHaveTextContent(/Camera data is \d+ min old/)
    expect(screen.getByTestId('big-count').closest('section')).not.toHaveAttribute('data-dimmed')
    expect(screen.getByTestId('zone-ground')).toHaveClass('dimmed')
    expect(screen.getByTestId('zone-ground')).toHaveTextContent('No fresh camera data')
    expect(screen.getByTestId('zone-underground')).not.toHaveClass('dimmed')
  })
})
