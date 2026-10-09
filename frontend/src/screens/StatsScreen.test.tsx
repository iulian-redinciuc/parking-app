import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as client from '../api/client'
import { ApiRequestError } from '../api/client'
import StatsScreen from './StatsScreen'

// P7.7 on the mock build: the forecast card, the today chart's table and the heatmap's table
// (the screen-reader fallbacks), switching zones, and the not-enough-data / error states.
describe('StatsScreen', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows the forecast, today vs typical and the heatmap as tables', async () => {
    render(<StatsScreen />)
    expect(await screen.findByText(/^Usually ~\d+ free at /)).toBeInTheDocument()
    expect(screen.getByText('From the same time over the last 8 weeks')).toBeInTheDocument()
    const heatmap = screen.getByRole('table', { name: /Average free spaces by weekday/ })
    // 7 weekdays + the header row, each with 24 hours
    expect(within(heatmap).getAllByRole('row')).toHaveLength(8)
    expect(within(heatmap).getByRole('rowheader', { name: 'Monday' })).toBeInTheDocument()
    const today = screen.getByRole('table', { name: 'Today' })
    // the typical band covers every hour of 8 weeks of mock data
    expect(within(today).getAllByRole('row')).toHaveLength(25)
    expect(screen.getByTestId('heatmap').getAttribute('aria-hidden')).toBe('true')
  })

  it('switches zones', async () => {
    const spy = vi.spyOn(client, 'getForecast')
    render(<StatsScreen />)
    const ground = await screen.findByRole('button', { name: 'Ground' })
    expect(screen.getByRole('button', { name: 'All zones' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    fireEvent.click(ground)
    expect(ground).toHaveAttribute('aria-pressed', 'true')
    await screen.findByText(/^Usually ~\d+ free at /)
    expect(spy).toHaveBeenLastCalledWith(
      expect.objectContaining({ zone: 'ground' }),
      expect.anything(),
    )
  })

  it('says when there is not enough history for a forecast', async () => {
    vi.spyOn(client, 'getForecast').mockRejectedValue(
      new ApiRequestError({ code: 'not_enough_data', message: 'x', status: 404 }),
    )
    render(<StatsScreen />)
    expect(await screen.findByText(/Not enough history yet/)).toBeInTheDocument()
  })

  it('offers a retry after an error', async () => {
    const spy = vi
      .spyOn(client, 'getHistory')
      .mockRejectedValueOnce(new ApiRequestError({ code: 'network', message: 'x' }))
    render(<StatsScreen />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load the stats")
    spy.mockRestore()
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText(/^Usually ~\d+ free at /)).toBeInTheDocument()
  })
})
