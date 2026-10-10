import {
  act,
  fireEvent,
  render as renderDom,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adminLogin, getAdminAlerts, MOCK_ADMIN_PASSWORD, setAdminToken } from '../api/client'
import * as push from '../lib/push'
import NotificationsScreen from './NotificationsScreen'

// the screen links to #/privacy, so it needs a router around it
const render = (ui: ReactElement) => renderDom(ui, { wrapper: MemoryRouter })

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
    geolocation: {
      getCurrentPosition: vi.fn((ok: PositionCallback) => ok({} as GeolocationPosition)),
    },
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

  it("keeps 'near' on this device without push (iPhone Safari outside the app)", async () => {
    stubPush()
    const getCurrentPosition = vi.fn((ok: PositionCallback) => ok({} as GeolocationPosition))
    vi.stubGlobal('navigator', {
      ...navigator,
      userAgent: IPHONE_SAFARI,
      maxTouchPoints: 5,
      geolocation: { getCurrentPosition },
    })
    render(<NotificationsScreen />)
    const near = await screen.findByRole('switch', { name: /Tell me when I'm near/ })
    await act(async () => fireEvent.click(near))
    expect(getCurrentPosition).toHaveBeenCalledOnce()
    expect(near).toBeChecked()
    expect(push.loadPrefs().proximity).toBe(true)
    expect(push.updatePrefs).not.toHaveBeenCalled()
    expect(await screen.findByText('Within 500 m of the lot')).toBeInTheDocument()
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

    it('offers admin alerts only to a logged-in admin', async () => {
      stubPush('granted')
      const { unmount } = render(<NotificationsScreen />)
      await screen.findByRole('switch', { name: /Warn when almost full/ })
      expect(screen.queryByRole('switch', { name: /Receive admin alerts/ })).toBeNull()
      unmount()
      await adminLogin(MOCK_ADMIN_PASSWORD)
      try {
        render(<NotificationsScreen />)
        const admin = await screen.findByRole('switch', { name: /Receive admin alerts/ })
        expect(await screen.findByText('None right now.')).toBeInTheDocument()
        expect(admin).not.toBeChecked()
        await act(async () => fireEvent.click(admin))
        expect(admin).toBeChecked()
        expect((await getAdminAlerts(push.MOCK_ENDPOINT)).enabled).toBe(true)
        act(() => setAdminToken(null)) // logged out: the setting goes away
        expect(screen.queryByRole('switch', { name: /Receive admin alerts/ })).toBeNull()
      } finally {
        setAdminToken(null)
      }
    })

    it('saves a burst of changes with one PATCH 500 ms after the last', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      const almostFull = await screen.findByRole('switch', { name: /Warn when almost full/ })
      vi.useFakeTimers()
      try {
        fireEvent.click(almostFull)
        act(() => void vi.advanceTimersByTime(400))
        // 'near' asks for the location first, so its change lands a microtask later
        await act(async () =>
          fireEvent.click(screen.getByRole('switch', { name: /Tell me when I'm near/ })),
        )
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

    it('follows the special spaces the lot has', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      // the mock lot has accessible spaces and EV chargers, nothing else
      const group = within(await screen.findByRole('group', { name: 'Special spaces' }))
      expect(group.getAllByRole('checkbox')).toHaveLength(2)
      fireEvent.click(group.getByRole('checkbox', { name: 'EV charging' }))
      fireEvent.click(group.getByRole('checkbox', { name: 'Accessible' }))
      await waitFor(() => expect(push.updatePrefs).toHaveBeenCalledOnce())
      expect(vi.mocked(push.updatePrefs).mock.calls[0][0].space_types).toEqual(['accessible', 'ev'])
      expect(group.getByRole('checkbox', { name: 'EV charging' })).toBeChecked()
      fireEvent.click(group.getByRole('checkbox', { name: 'Accessible' }))
      await waitFor(() => expect(push.loadPrefs().space_types).toEqual(['ev']))
    })

    it('adds and removes a reminder, sets quiet hours and filters zones', async () => {
      stubPush('granted')
      render(<NotificationsScreen />)
      fireEvent.click(await screen.findByRole('button', { name: 'Add reminder' }))
      expect(screen.getByText('Mon, Tue, Wed, Thu, Fri at 08:30')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('switch', { name: /Quiet hours/ }))
      expect(screen.getByLabelText('From')).toHaveValue('22:00')
      const zones = within(await screen.findByRole('group', { name: 'Zones' })).getAllByRole(
        'checkbox',
      )
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

    it("asks for the location when 'near' is switched on, and stays off when refused", async () => {
      stubPush('granted')
      const getCurrentPosition = vi.fn(
        (_ok: PositionCallback, fail?: PositionErrorCallback | null) =>
          fail?.({ code: 1 } as GeolocationPositionError),
      )
      vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } })
      render(<NotificationsScreen />)
      const near = await screen.findByRole('switch', { name: /Tell me when I'm near/ })
      await act(async () => fireEvent.click(near))
      expect(getCurrentPosition).toHaveBeenCalledOnce()
      expect(await screen.findByText(/Location is blocked/)).toBeInTheDocument()
      expect(near).not.toBeChecked()
    })

    it('sends a test notification', async () => {
      const { reg } = stubPush('granted')
      render(<NotificationsScreen />)
      const button = await screen.findByRole('button', { name: 'Send test notification' })
      await act(async () => fireEvent.click(button))
      expect(reg.showNotification).toHaveBeenCalledOnce()
      expect(await screen.findByText(/It should arrive in a few seconds/)).toBeInTheDocument()
    })

    it("I'm on my way: a chip starts the countdown, Stop updates ends it", async () => {
      const { reg } = stubPush('granted')
      render(<NotificationsScreen />)
      const chip = await screen.findByRole('button', { name: '15 min' })
      expect(screen.getByRole('button', { name: '30 min' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '60 min' })).toBeInTheDocument()
      vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
      try {
        await act(async () => fireEvent.click(chip))
        expect(reg.showNotification).toHaveBeenCalledOnce() // mock: the first update, locally
        expect(screen.getByRole('timer')).toHaveTextContent('15:00 left')
        expect(screen.getByText(/^Updates until /)).toBeInTheDocument()
        act(() => void vi.advanceTimersByTime(61_000))
        expect(screen.getByRole('timer')).toHaveTextContent('13:59 left')
        expect(screen.queryByRole('button', { name: '15 min' })).toBeNull()
        await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Stop updates' })))
        expect(screen.queryByRole('timer')).toBeNull()
        expect(push.onMyWayUntil()).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })

    it("I'm on my way: the chips come back when the window ends, also after a reload", async () => {
      stubPush('granted')
      localStorage.setItem('parking.onMyWayUntil', String(Date.now() + 2_000))
      render(<NotificationsScreen />)
      expect(await screen.findByRole('timer')).toHaveTextContent(/0:0[12] left/)
      expect(
        await screen.findByRole('button', { name: '15 min' }, { timeout: 4000 }),
      ).toBeInTheDocument()
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
