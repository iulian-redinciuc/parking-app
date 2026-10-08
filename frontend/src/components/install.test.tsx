import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { canInstall, startInstallCapture } from '../lib/install'
import InstallButton from './InstallButton'
import InstallHint from './InstallHint'

const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'

function fireInstallPrompt(outcome: 'accepted' | 'dismissed' = 'accepted') {
  const event = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), {
    prompt: vi.fn(() => Promise.resolve()),
    userChoice: Promise.resolve({ outcome }),
  })
  act(() => void window.dispatchEvent(event))
  return event
}

describe('InstallButton', () => {
  beforeEach(() => startInstallCapture())
  afterEach(() => act(() => void window.dispatchEvent(new Event('appinstalled'))))

  it('is hidden until the browser offers installing', () => {
    render(<InstallButton />)
    expect(screen.queryByRole('button', { name: 'Install app' })).toBeNull()
  })

  it('shows the saved prompt once, then hides', async () => {
    render(<InstallButton />)
    const event = fireInstallPrompt()
    expect(event.defaultPrevented).toBe(true) // no mini-infobar
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Install app' })))
    expect(event.prompt).toHaveBeenCalledOnce()
    expect(canInstall()).toBe(false)
    expect(screen.queryByRole('button', { name: 'Install app' })).toBeNull()
  })

  it('hides after the app is installed some other way', () => {
    render(<InstallButton />)
    fireInstallPrompt()
    expect(screen.getByRole('button', { name: 'Install app' })).toBeInTheDocument()
    act(() => void window.dispatchEvent(new Event('appinstalled')))
    expect(screen.queryByRole('button', { name: 'Install app' })).toBeNull()
  })
})

describe('InstallHint', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.unstubAllGlobals())

  const asIphone = () =>
    vi.stubGlobal('navigator', { ...navigator, userAgent: IPHONE_SAFARI, maxTouchPoints: 5 })

  it('is not shown outside iOS Safari', () => {
    render(<InstallHint />)
    expect(screen.queryByTestId('install-hint')).toBeNull()
  })

  it('tells iPhone Safari users how to install, and can be hidden for good', () => {
    asIphone()
    const { unmount } = render(<InstallHint />)
    expect(screen.getByText('Add Parking to your Home Screen')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Hide this tip' }))
    expect(screen.queryByTestId('install-hint')).toBeNull()
    unmount()
    render(<InstallHint />)
    expect(screen.queryByTestId('install-hint')).toBeNull()
  })

  it('`always` ignores an earlier dismissal and has no close button', () => {
    asIphone()
    localStorage.setItem('parking.installHintDismissed', '1')
    render(<InstallHint always />)
    expect(screen.getByTestId('install-hint')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Hide this tip' })).toBeNull()
  })

  it('is not shown when opened from the home screen', () => {
    asIphone()
    vi.stubGlobal('navigator', { ...navigator, standalone: true })
    render(<InstallHint />)
    expect(screen.queryByTestId('install-hint')).toBeNull()
  })
})
