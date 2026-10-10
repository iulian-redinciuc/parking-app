import { act, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import statusJson from '../api/__fixtures__/lot-status.json'
import type { Level, LotStatus, Totals, Trend, ZoneStatus } from '../api/types'
import BigCount, { ANNOUNCE_EVERY_MS } from './BigCount'
import TrendIcon from './TrendIcon'
import { formatUpdatedAgo } from '../lib/time'
import UpdatedAgo from './UpdatedAgo'
import ZoneCard from './ZoneCard'

const status = statusJson as LotStatus
const total = (over: Partial<Totals> = {}): Totals => ({ ...status.total, ...over })
const zone = (over: Partial<ZoneStatus> = {}): ZoneStatus => ({ ...status.zones[0], ...over })

function renderZone(z: ZoneStatus) {
  render(
    <ul>
      <ZoneCard zone={z} />
    </ul>,
  )
  return screen.getByRole('listitem')
}

afterEach(() => vi.useRealTimers())

describe('BigCount', () => {
  it('shows the free count, the unit, the level word and a bar', () => {
    const { container } = render(<BigCount total={total()} />)
    expect(screen.getByTestId('big-count')).toHaveTextContent(/^23$/)
    expect(screen.getByText('free spaces')).toBeInTheDocument()
    expect(screen.getByText('Plenty of space')).toHaveClass('text-ok')
    expect(container.querySelector('[data-percent]')).toHaveAttribute('data-percent', '77')
  })

  it.each([
    ['plenty', 'Plenty of space', 'text-ok', 'bg-ok'],
    ['filling', 'Filling up', 'text-warn', 'bg-warn'],
    ['almost_full', 'Almost full', 'text-bad', 'bg-bad'],
    ['full', 'Full', 'text-bad', 'bg-bad'],
  ] as const)('shows level %s as a word and a colour', (level, word, text, bg) => {
    const { container } = render(<BigCount total={total({ level: level as Level })} />)
    expect(screen.getByText(word)).toHaveClass(text)
    expect(container.querySelector('[data-percent]')).toHaveClass(bg)
  })

  it('prefixes ≈ when the lowest confidence is below 0.8, and says "1 free space"', () => {
    render(<BigCount total={total({ free: 1, confidence: 0.5 })} />)
    expect(screen.getByTestId('big-count')).toHaveTextContent('≈ 1')
    expect(screen.getByText('free space')).toBeInTheDocument()
  })

  it('announces through aria-live at most every 30 s, ending on the latest count', () => {
    vi.useFakeTimers()
    const { rerender } = render(<BigCount total={total()} />)
    const live = document.querySelector('[aria-live="polite"]')!
    expect(live).toHaveTextContent('23 free spaces, Plenty of space')

    rerender(<BigCount total={total({ free: 22 })} />)
    act(() => vi.advanceTimersByTime(0))
    expect(live).toHaveTextContent('22 free spaces') // first change goes out at once
    expect(screen.getByTestId('big-count')).toHaveTextContent('22')

    rerender(<BigCount total={total({ free: 21 })} />)
    rerender(<BigCount total={total({ free: 20 })} />)
    expect(screen.getByTestId('big-count')).toHaveTextContent('20') // the screen doesn't wait
    act(() => vi.advanceTimersByTime(ANNOUNCE_EVERY_MS - 1))
    expect(live).toHaveTextContent('22 free spaces')
    act(() => vi.advanceTimersByTime(1))
    expect(live).toHaveTextContent('20 free spaces')
  })
})

describe('ZoneCard', () => {
  it('shows the name, free / capacity, the level word and the trend', () => {
    const card = renderZone(zone())
    expect(within(card).getByRole('heading', { name: 'Ground' })).toBeInTheDocument()
    expect(card).toHaveAccessibleName('Ground')
    expect(card).toHaveTextContent('12 / 40 free')
    expect(within(card).getByText('Plenty of space')).toHaveClass('text-ok')
    expect(within(card).getByText('Trend: getting fuller')).toBeInTheDocument()
    expect(card).not.toHaveTextContent('Estimated')
  })

  it.each([
    ['filling', 'Filling up', 'text-warn'],
    ['almost_full', 'Almost full', 'text-bad'],
    ['full', 'Full', 'text-bad'],
  ] as const)('shows level %s', (level, word, cls) => {
    const card = renderZone(zone({ level }))
    expect(within(card).getByText(word)).toHaveClass(cls)
  })

  it('shows a chip per kind of special space, with words for screen readers', () => {
    const card = renderZone(zone())
    const chips = within(card).getByRole('group', { name: 'Special spaces' })
    expect(chips.children).toHaveLength(2)
    expect(within(card).getByTestId('special-accessible')).toHaveTextContent('Accessible 1 / 2')
    expect(within(card).getByText('Accessible: 1 of 2 free')).toHaveClass('sr-only')
    expect(within(card).getByTestId('special-ev')).toHaveTextContent('EV charging 0 / 2')
    expect(within(card).getByTestId('special-ev')).toHaveAttribute('data-free', '0')
  })

  it.each([[{}], [null], [undefined]])('shows no chips for by_type %j', (by_type) => {
    const card = renderZone(zone({ by_type }))
    expect(within(card).queryByRole('group', { name: 'Special spaces' })).toBeNull()
  })

  it('skips a space type this app version does not know', () => {
    const by_type = { accessible: { capacity: 1, free: 1 }, taxi: { capacity: 3, free: 2 } }
    const card = renderZone(zone({ by_type } as Partial<ZoneStatus>))
    expect(within(card).getByRole('group', { name: 'Special spaces' }).children).toHaveLength(1)
  })

  it('marks a low-confidence flow zone with ≈ and an Estimated note', () => {
    const card = renderZone({ ...status.zones[1], confidence: 0.6 })
    expect(card).toHaveTextContent('≈ 11 / 60')
    expect(card).toHaveTextContent('Estimated from entry/exit counts')
  })

  it('uses a camera note for a low-confidence slots zone', () => {
    const card = renderZone(zone({ confidence: 0.4 }))
    expect(card).toHaveTextContent('≈ 12 / 40')
    expect(card).toHaveTextContent('Estimated: the camera view is unclear')
  })
})

describe('TrendIcon', () => {
  it.each([
    ['filling', 'Trend: getting fuller', 'rotate-45'],
    ['emptying', 'Trend: emptying', '-rotate-45'],
    ['steady', 'Trend: steady', null],
  ] as const)('shows %s as an arrow with a text alternative', (trend, text, rotation) => {
    const { container } = render(<TrendIcon trend={trend as Trend} />)
    expect(screen.getByText(text)).toHaveClass('sr-only')
    const svg = container.querySelector('svg')!
    expect(svg).toHaveAttribute('aria-hidden', 'true')
    if (rotation) expect(svg).toHaveClass(rotation)
    else expect(svg.getAttribute('class')).not.toMatch(/rotate/)
  })
})

describe('UpdatedAgo', () => {
  it.each([
    [0, 'Updated just now'],
    [999, 'Updated just now'],
    [1_000, 'Updated 1 second ago'],
    [3_400, 'Updated 3 seconds ago'],
    [59_999, 'Updated 59 seconds ago'],
    [60_000, 'Updated 1 minute ago'],
    [125_000, 'Updated 2 minutes ago'],
    [3 * 3600_000, 'Updated 3 hours ago'],
    [50 * 3600_000, 'Updated 2 days ago'],
    [-5_000, 'Updated just now'], // a clock that went backwards
  ])('%i ms → %s', (ago, text) => {
    expect(formatUpdatedAgo(1_000_000_000 - ago, 1_000_000_000)).toBe(text)
  })

  it('ticks every second', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    render(<UpdatedAgo at={1_000_000 - 3_000} />)
    expect(screen.getByText('Updated 3 seconds ago')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1_000))
    expect(screen.getByText('Updated 4 seconds ago')).toBeInTheDocument()
  })

  it('renders nothing before the first message', () => {
    const { container } = render(<UpdatedAgo at={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
