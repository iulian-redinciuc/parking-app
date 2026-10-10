import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as client from '../api/client'
import { lotPath, selectLot, selectedLot } from '../api/lots'
import type { LotList } from '../api/types'
import * as push from '../lib/push'
import LotPicker from './LotPicker'

const TWO: LotList = {
  v: 1,
  default: 'main',
  lots: [
    { id: 'main', name: 'Main street' },
    { id: 'north', name: 'North gate' },
  ],
}

function show(list: LotList | Error, reload = vi.fn()) {
  const getLots = vi.spyOn(client, 'getLots')
  if (list instanceof Error) getLots.mockRejectedValue(list)
  else getLots.mockResolvedValue(list)
  render(<LotPicker fallback={<span>Parking</span>} reload={reload} />)
  return reload
}

describe('lotPath', () => {
  afterEach(() => localStorage.clear())

  it('uses the paths without a lot until one is picked', () => {
    expect(lotPath('/status')).toBe('/api/status')
    expect(lotPath()).toBe('/api/lot')
    selectLot('north')
    expect(selectedLot()).toBe('north')
    expect(lotPath('/status')).toBe('/api/lots/north/status')
    expect(lotPath('/maps/ground')).toBe('/api/lots/north/maps/ground')
    expect(lotPath()).toBe('/api/lots/north')
    selectLot(null)
    expect(lotPath('/stream')).toBe('/api/stream')
  })

  it('ignores a stored value that is not a lot id', () => {
    localStorage.setItem('parking.lot', '../admin')
    expect(selectedLot()).toBeNull()
  })
})

describe('LotPicker', () => {
  let follow: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    follow = vi.spyOn(push, 'followSelectedLot').mockResolvedValue()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('shows the app name while the server has one lot', async () => {
    show({ ...TWO, lots: TWO.lots.slice(0, 1) })
    await waitFor(() => expect(client.getLots).toHaveBeenCalled())
    expect(screen.getByText('Parking')).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('shows the app name when the server has no lot list', async () => {
    show(new Error('404'))
    await waitFor(() => expect(client.getLots).toHaveBeenCalled())
    expect(screen.getByText('Parking')).toBeInTheDocument()
  })

  it('lists the lots with the default one chosen, and switches', async () => {
    const reload = show(TWO)
    const select = await screen.findByRole('combobox', { name: 'Car park' })
    expect(select).toHaveValue('main')
    expect(screen.queryByText('Parking')).toBeNull()

    fireEvent.change(select, { target: { value: 'north' } })
    expect(selectedLot()).toBe('north')
    expect(follow).toHaveBeenCalledOnce()
    await waitFor(() => expect(reload).toHaveBeenCalledOnce())
  })

  it('opens on the lot picked earlier', async () => {
    selectLot('north')
    show(TWO)
    expect(await screen.findByRole('combobox')).toHaveValue('north')
  })

  it('goes back to the default lot when the picked one is gone', async () => {
    selectLot('closed')
    const reload = show(TWO)
    await waitFor(() => expect(reload).toHaveBeenCalledOnce())
    expect(selectedLot()).toBeNull()
    expect(follow).toHaveBeenCalledOnce()
  })
})
