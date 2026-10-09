import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { getAdminToken, MOCK_ADMIN_PASSWORD, setAdminToken } from '../../api/client'
import AdminScreen from './AdminScreen'

function renderAdmin(path = '/admin') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="admin/*" element={<AdminScreen />} />
      </Routes>
    </MemoryRouter>,
  )
}

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
    renderAdmin()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled()
    await signIn('nope')
    expect(screen.getByRole('alert')).toHaveTextContent('Wrong password.')
    expect(getAdminToken()).toBeNull()
  })

  it('signs in, keeps the token in sessionStorage, and logs out', async () => {
    renderAdmin()
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
    renderAdmin()
    await act(async () => {})
    expect(getAdminToken()).toBeNull()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })
})

// P7.2 on the mock build: the camera list and a camera's snapshot through a blob URL.
describe('admin cameras', () => {
  beforeAll(() => {
    // jsdom has no blob URLs
    URL.createObjectURL ??= () => ''
    URL.revokeObjectURL ??= () => undefined
  })

  afterEach(() => {
    vi.restoreAllMocks()
    setAdminToken(null)
    sessionStorage.clear()
  })

  async function signedIn(path: string) {
    renderAdmin(path)
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: MOCK_ADMIN_PASSWORD },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await act(async () => {})
  }

  it('lists the cameras with their health; a row opens the detail', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:snap-1')
    await signedIn('/admin')
    const ground = await screen.findByRole('link', { name: /cam-ground/ })
    expect(ground).toHaveTextContent('OK')
    expect(ground).toHaveTextContent('0.2 fps')
    expect(ground).toHaveTextContent('151 ms per frame')
    expect(screen.getByRole('link', { name: /cam-ramp/ })).toHaveTextContent('No reports yet')

    fireEvent.click(ground)
    await act(async () => {})
    const image = await screen.findByRole('img', { name: /cam-ground with slots/ })
    expect(image).toHaveAttribute('src', 'blob:snap-1')
  })

  it('shows why there is no snapshot, and takes a new one on request', async () => {
    await signedIn('/admin/cameras/cam-ramp')
    expect(await screen.findByRole('alert')).toHaveTextContent(/No snapshot right now/)
    fireEvent.click(screen.getByRole('button', { name: 'New snapshot' }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('the annotated switch asks for the plain picture', async () => {
    const spy = vi
      .spyOn(URL, 'createObjectURL')
      .mockReturnValueOnce('blob:snap-2')
      .mockReturnValueOnce('blob:snap-3')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    await signedIn('/admin/cameras/cam-ground')
    await screen.findByRole('img', { name: /with slots/ })
    fireEvent.click(screen.getByLabelText('Show slots and detections'))
    await act(async () => {})
    expect(
      await screen.findByRole('img', { name: 'Current picture from cam-ground' }),
    ).toBeVisible()
    expect(spy).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:snap-3')
    expect(revoke).toHaveBeenCalledWith('blob:snap-2') // the old picture's URL is freed
  })
})
