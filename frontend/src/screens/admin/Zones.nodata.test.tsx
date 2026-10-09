import { act, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adminLogin, MOCK_ADMIN_PASSWORD, setAdminToken } from '../../api/client'
import Zones from './Zones'

// P7.4: before the API has any data `/api/status` is 503; the zones then come from `/api/lot`,
// so the first count can still be set.
vi.mock('../../hooks/useLiveStatus', () => ({
  useLiveStatus: () => ({
    status: null,
    connection: 'live',
    lastMessageAt: null,
    error: { code: 'unavailable', message: 'no data yet', status: 503 },
  }),
}))

describe('Zones before the first data', () => {
  beforeEach(async () => {
    await adminLogin(MOCK_ADMIN_PASSWORD)
  })
  afterEach(() => {
    setAdminToken(null)
    sessionStorage.clear()
  })

  it('offers the flow zones from /api/lot without a live count', async () => {
    render(
      <MemoryRouter>
        <Zones />
      </MemoryRouter>,
    )
    await act(async () => {})
    const form = screen.getByRole('form', { name: 'Underground' })
    expect(form).toHaveTextContent('No count yet (capacity 60).')
    expect(within(form).getByLabelText('Real number of cars')).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Ground' })).toBeNull()
  })
})
