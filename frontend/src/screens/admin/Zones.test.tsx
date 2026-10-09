import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { adminLogin, MOCK_ADMIN_PASSWORD, setAdminToken } from '../../api/client'
import { useLiveStatus } from '../../hooks/useLiveStatus'
import Zones from './Zones'

// P7.4 on the mock build: the underground (flow) zone can be corrected; the mock feed shows the
// new count at once (as SSE does live) and the correction lands in the log. Both components
// share the app's one mock feed, which updates only every 3–8 s.
function Underground() {
  const { status } = useLiveStatus()
  const zone = status?.zones.find((z) => z.id === 'underground')
  return <p data-testid="live">{zone?.occupied}</p>
}

describe('Zones (count corrections)', () => {
  beforeEach(async () => {
    await adminLogin(MOCK_ADMIN_PASSWORD)
  })
  afterEach(() => {
    setAdminToken(null)
    sessionStorage.clear()
  })

  function renderZones() {
    return render(
      <MemoryRouter>
        <Underground />
        <Zones />
      </MemoryRouter>,
    )
  }

  it('saves a correction, updates the live count and logs it', async () => {
    renderZones()
    await act(async () => {})
    expect(await screen.findByText('No corrections yet.')).toBeInTheDocument()
    const form = screen.getByRole('form', { name: 'Underground' })
    const before = Number(screen.getByTestId('live').textContent)
    const target = before === 12 ? 13 : 12

    fireEvent.change(within(form).getByLabelText('Real number of cars'), {
      target: { value: String(target) },
    })
    fireEvent.change(within(form).getByLabelText('Note (optional)'), {
      target: { value: 'counted on foot' },
    })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await act(async () => {})

    expect(within(form).getByRole('status')).toHaveTextContent(
      `Saved: Underground now has ${target} taken.`,
    )
    expect(screen.getByTestId('live')).toHaveTextContent(String(target))
    const log = screen.getByRole('list')
    expect(log).toHaveTextContent(`Underground: ${before} → ${target}`)
    expect(log).toHaveTextContent('counted on foot')
    expect(log).toHaveTextContent('sign-in #mock')
  })

  it('refuses a number above the capacity without calling the API', async () => {
    renderZones()
    await act(async () => {})
    const form = await screen.findByRole('form', { name: 'Underground' })
    fireEvent.change(within(form).getByLabelText('Real number of cars'), {
      target: { value: '61' },
    })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await act(async () => {})
    expect(within(form).getByRole('alert')).toHaveTextContent('Enter a whole number from 0 to 60.')
  })

  it('offers no form for zones counted from the picture', async () => {
    renderZones()
    await act(async () => {})
    expect(screen.queryByRole('form', { name: 'Ground' })).toBeNull()
  })
})
