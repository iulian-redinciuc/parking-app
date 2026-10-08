import { act, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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
    expect(screen.getByText('Waiting for data…')).toBeInTheDocument()

    push({ status: statusJson as LotStatus, connection: 'live', lastMessageAt: Date.now() })
    expect(screen.getByTestId('big-count')).toHaveTextContent('23')
    const zones = within(screen.getByRole('list', { name: 'Zones' })).getAllByRole('listitem')
    expect(zones.map((z) => z.getAttribute('aria-labelledby'))).toEqual([
      'zone-ground',
      'zone-underground',
    ])
    expect(screen.getByText(/^Updated/)).toBeInTheDocument()
  })
})
