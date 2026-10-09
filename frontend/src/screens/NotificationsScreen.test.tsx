import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as push from '../lib/push'
import NotificationsScreen from './NotificationsScreen'

// vitest runs without VITE_API_BASE, so the screen talks to the mock: subscribing only asks for
// permission and the test notification is shown by the service worker stub.

vi.mock('../lib/push', async (importOriginal) => {
  const actual = await importOriginal<typeof push>()
  return { ...actual, updatePrefs: vi.fn(actual.updatePrefs) }
})

const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'

function stubPush(permission: NotificationPermission = 'default', answer = permission) {
  const reg = {
    scope: 'http://localhost/',
    pushManager: { getSubscription: async () => null },
    showNotification: vi.fn(async () => undefined),
  }
  vi.stubGlobal('navigator', {
    ...navigator,
    serviceWorker: { getRegistration: async () => reg, ready: Promise.resolve(reg) },
  })
  vi.stubGlobal('PushManager', class {})
  const Notification = Object.assign(function () {}, {
    permission,
    requestPermission: vi.fn(async () => {
      Notification.permission = answer
      return answer
    }),
  })
  vi.stubGlobal('Notification', Notification)
  return { reg, Notification }
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  vi.unstubAllGlobals()
  vi.mocked(push.updatePrefs).mockClear()
})

describe('NotificationsScreen', () => {
  it('says so when the browser has no Web Push', async () => {
    render(<NotificationsScreen />)
    expect(await screen.findByText(/This browser can't show notifications/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Enable notifications' })).toBeNull()
  })

  it('shows the install hint on iPhone Safari outside the installed app', async () => {
    stubPush()
    vi.stubGlobal('navigator', { ...navigator, userAgent: IPHONE_SAFARI, maxTouchPoints: 5 })
    render(<NotificationsScreen />)
    expect(await screen.findByTestId('install-hint')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Enable notifications' })).toBeNull()
  })

  it('explains how to unblock when permission was denied', async () => {
    stubPush('denied')
    render(<NotificationsScreen />)
    expect(await screen.findByText('Notifications are blocked')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Enable notifications' })).toBeNull()
  })

  it('asks for permission only on the tap, then shows the settings', async () => {
    const { Notification } = stubPush('default', 'granted')
    render(<NotificationsScreen />)
    const enable = await screen.findByRole('button', { name: 'Enable notifications' })
    expect(Notification.requestPermission).not.toHaveBeenCalled()
    await act(async () => fireEvent.click(enable))
    expect(Notification.requestPermission).toHaveBeenCalledOnce()
    expect(await screen.findByText('Notifications are on for this device.')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: /Warn when almost full/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send test notification' })).toBeInTheDocument()
  })

  it('a refused prompt switches to the blocked explanation', async () => {
    stubPush('default', 'denied')
    render(<NotificationsScreen />)
    const button = await screen.findByRole('button', { name: 'Enable notifications' })
    await act(async () => fireEvent.click(button))
    expect(await screen.findByText('Notifications are blocked')).toBeInTheDocument()
  })

  describe('when subscribed', () => {
    beforeEach(() => localStorage.setItem('parking.pushEndpoint', push.MOCK_ENDPOINT))

    it('saves a burst of changes with one PATCH 500 ms after the last', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      const almostFull = await screen.findByRole('switch', { name: /Warn when almost full/ })
      vi.useFakeTimers()
      try {
        fireEvent.click(almostFull)
        act(() => void vi.advanceTimersByTime(400))
        fireEvent.click(screen.getByRole('switch', { name: /Tell me when I'm near/ }))
        act(() => void vi.advanceTimersByTime(400))
        expect(push.updatePrefs).not.toHaveBeenCalled()
        await act(async () => void vi.advanceTimersByTime(100))
      } finally {
        vi.useRealTimers()
      }
      expect(push.updatePrefs).toHaveBeenCalledOnce()
      expect(vi.mocked(push.updatePrefs).mock.calls[0][0]).toMatchObject({
        alert_when_almost_full: true,
        proximity: true,
      })
      expect(await screen.findByText('Saved')).toBeInTheDocument()
      expect(push.loadPrefs()).toMatchObject({ alert_when_almost_full: true, proximity: true })
    })

    it('adds and removes a reminder, sets quiet hours and filters zones', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      fireEvent.click(await screen.findByRole('button', { name: 'Add reminder' }))
      expect(screen.getByText('Mon, Tue, Wed, Thu, Fri at 08:30')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('switch', { name: /Quiet hours/ }))
      expect(screen.getByLabelText('From')).toHaveValue('22:00')
      const zones = await screen.findAllByRole('checkbox')
      fireEvent.click(zones[0])
      await waitFor(() => expect(push.updatePrefs).toHaveBeenCalledOnce())
      const saved = vi.mocked(push.updatePrefs).mock.calls[0][0]
      expect(saved.schedules).toEqual([{ days: [1, 2, 3, 4, 5], time: '08:30' }])
      expect(saved.quiet_hours).toEqual({ from: '22:00', to: '07:00' })
      expect(saved.zones).toHaveLength(zones.length - 1)

      fireEvent.click(screen.getByRole('button', { name: /^Remove Mon/ }))
      await waitFor(() => expect(push.updatePrefs).toHaveBeenCalledTimes(2))
      expect(vi.mocked(push.updatePrefs).mock.calls[1][0].schedules).toEqual([])
    })

    it('sends a test notification', async () => {
      const { reg } = stubPush('granted')
      render(<NotificationsScreen />)
      const button = await screen.findByRole('button', { name: 'Send test notification' })
      await act(async () => fireEvent.click(button))
      expect(reg.showNotification).toHaveBeenCalledOnce()
      expect(await screen.findByText(/It should arrive in a few seconds/)).toBeInTheDocument()
    })

    it('turns notifications off', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      const button = await screen.findByRole('button', { name: 'Turn off notifications' })
      await act(async () => fireEvent.click(button))
      expect(
        await screen.findByRole('button', { name: 'Enable notifications' }),
      ).toBeInTheDocument()
      expect(push.storedEndpoint()).toBeNull()
    })
  })
})
