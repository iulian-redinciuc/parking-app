import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { getAdminToken, MOCK_ADMIN_PASSWORD, setAdminToken } from '../../api/client'
import AdminScreen from './AdminScreen'

// P7.1 on the mock build (no API): the mock accepts only MOCK_ADMIN_PASSWORD.
describe('AdminScreen', () => {
  afterEach(() => {
    setAdminToken(null)
    sessionStorage.clear()
  })

  async function signIn(password: string) {
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await act(async () => {})
  }

  it('shows the login without a token; a wrong password says so', async () => {
    render(<AdminScreen />)
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled()
    await signIn('nope')
    expect(screen.getByRole('alert')).toHaveTextContent('Wrong password.')
    expect(getAdminToken()).toBeNull()
  })

  it('signs in, keeps the token in sessionStorage, and logs out', async () => {
    render(<AdminScreen />)
    await signIn(MOCK_ADMIN_PASSWORD)
    expect(sessionStorage.getItem('parking.adminToken')).toBeTruthy()
    expect(localStorage.getItem('parking.adminToken')).toBeNull()
    expect(await screen.findByText('Signed in')).toBeInTheDocument()
    expect(screen.getByText(/^Signed in until /)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
    await act(async () => {})
    expect(getAdminToken()).toBeNull()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('a token the server no longer accepts goes back to the login', async () => {
    setAdminToken('expired-token')
    render(<AdminScreen />)
    await act(async () => {})
    expect(getAdminToken()).toBeNull()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })
})
