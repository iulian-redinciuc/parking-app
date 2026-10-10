import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MockInstance } from 'vitest'
import statusJson from '../api/__fixtures__/lot-status.json'
import * as client from '../api/client'
import type { LotStatus, ZoneStatus } from '../api/types'
import SlotMap from './SlotMap'
import ZoneCard from './ZoneCard'

const status = statusJson as LotStatus
const ground = (over: Partial<ZoneStatus> = {}): ZoneStatus => ({ ...status.zones[0], ...over })
const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">
  <rect id="G01" x="0" y="0" width="30" height="50"/>
  <rect id="G02" x="35" y="0" width="30" height="50"/>
  <rect id="G03" x="70" y="0" width="30" height="50"/>
  <line x1="0" y1="25" x2="100" y2="25"/>
</svg>`

let getSlotMap: MockInstance<typeof client.getSlotMap>

beforeEach(() => {
  localStorage.clear()
  getSlotMap = vi.spyOn(client, 'getSlotMap').mockResolvedValue(SVG)
})
afterEach(() => vi.restoreAllMocks())

async function openMap(zone: ZoneStatus) {
  const view = render(<SlotMap zone={zone} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Show map' }))
  return view
}

describe('SlotMap', () => {
  it('stays closed until asked for, then colours each space from the status', async () => {
    await openMap(ground())
    expect(getSlotMap).toHaveBeenCalledWith('ground', expect.anything())
    expect(screen.getByRole('button', { name: 'Hide map' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    const map = screen.getByRole('group', { name: 'Map of Ground' })
    expect(map).toHaveAttribute('viewBox', '0 0 100 50')
    const taken = within(map).getByRole('button', { name: 'Space G01: Taken' })
    const free = within(map).getByRole('button', { name: 'Space G02: Free' })
    expect(taken).toHaveAttribute('data-state', 'taken')
    expect(free).toHaveAttribute('data-state', 'free')
    expect(free).toHaveClass('text-ok')
    expect(taken).not.toHaveClass('text-ok')
    // on the map but not in the status
    expect(within(map).getByRole('button', { name: 'Space G03: No data' })).toBeInTheDocument()
    // the lane is drawn but is no space
    expect(map.querySelectorAll('line')).toHaveLength(1)
    expect(within(map).getAllByRole('button')).toHaveLength(3)
    expect(screen.getByRole('status')).toHaveTextContent('Tap a space to see its number')
  })

  it('names the space that was tapped, by touch or keyboard', async () => {
    await openMap(ground())
    const free = screen.getByRole('button', { name: 'Space G02: Free' })
    fireEvent.click(free)
    expect(screen.getByRole('status')).toHaveTextContent('Space G02: Free')
    expect(free).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(free)
    expect(screen.getByRole('status')).toHaveTextContent('Tap a space')
    fireEvent.keyDown(screen.getByRole('button', { name: 'Space G01: Taken' }), { key: 'Enter' })
    expect(screen.getByRole('status')).toHaveTextContent('Space G01: Taken')
  })

  it('follows the live status', async () => {
    const { rerender } = await openMap(ground())
    fireEvent.click(screen.getByRole('button', { name: 'Space G02: Free' }))
    rerender(<SlotMap zone={ground({ slots: { G01: false, G02: true } })} />)
    expect(screen.getByRole('button', { name: 'Space G01: Free' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Space G02: Taken')
    expect(getSlotMap).toHaveBeenCalledTimes(1)
  })

  it('says how many spaces the map lacks', async () => {
    await openMap(ground({ slots: { G01: true, G02: false, G40: false, G41: true } }))
    expect(screen.getByText("2 spaces aren't on the map")).toBeInTheDocument()
  })

  it('remembers that the map was open', async () => {
    const first = await openMap(ground())
    first.unmount()
    render(<SlotMap zone={ground()} />)
    expect(await screen.findByRole('group', { name: 'Map of Ground' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Hide map' }))
    expect(screen.queryByRole('group')).not.toBeInTheDocument()
    expect(localStorage.getItem('parking.map.open.ground')).toBe('0')
  })

  it.each([
    ['the zone has no map', () => Promise.resolve(null)],
    ['the map is not an SVG', () => Promise.resolve('<html/>')],
    ['the map has no spaces', () => Promise.resolve(SVG.replace(/<rect[^>]*>/g, ''))],
    ['the request fails', () => Promise.reject(new Error('offline'))],
  ])('shows nothing when %s', async (_, answer) => {
    getSlotMap.mockImplementation(answer)
    const { container } = render(<SlotMap zone={ground()} />)
    await waitFor(() => expect(getSlotMap).toHaveBeenCalled())
    await act(async () => {})
    expect(container).toBeEmptyDOMElement()
  })

  it('is not asked for on a zone without slots', async () => {
    render(
      <ul>
        <ZoneCard zone={status.zones[1]} />
      </ul>,
    )
    await act(async () => {})
    expect(getSlotMap).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Show map' })).not.toBeInTheDocument()
  })

  it('sits on the card of a zone with slots', async () => {
    render(
      <ul>
        <ZoneCard zone={ground()} />
      </ul>,
    )
    const card = screen.getByRole('listitem')
    expect(await within(card).findByRole('button', { name: 'Show map' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })
})
