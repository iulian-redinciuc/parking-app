// Web Push on the page (notifications.md §2): permission, subscribing, prefs and the test push.
// The service worker side is `swPush.ts`. With `VITE_API_BASE=mock` (dev, the Pages preview,
// e2e) nothing is sent anywhere: subscribing only asks for permission and the test notification
// is shown locally from the mock status, so the whole flow can be tried without a server.
import { API_BASE } from '../api/base'
import { mockStatus } from '../api/mock'
import { getRequest, sendJson } from '../api/client'
import type { LotStatus, SpaceType } from '../api/types'
import i18n, { t } from '../i18n'
import { roundDistance } from './geo'
import { specialTotals } from './status'
import { isIosSafari, isStandalone } from './pwa'
import {
  clearPushRegistration,
  notificationFromPush,
  savePushRegistration,
  STATUS_TAG,
  urlBase64ToUint8Array,
} from './swPush'

/** `Prefs` of api.md §2 (backend `parking/api/routes/push.py`). */
export interface Schedule {
  /** ISO weekdays, 1 = Monday … 7 = Sunday. */
  days: number[]
  /** `HH:MM` in the subscription's time zone. */
  time: string
}

export interface QuietHours {
  from: string
  to: string
}

export interface Prefs {
  proximity: boolean
  /** `null` = the lot's `notify_radius_m`. */
  radius_m: number | null
  /** `null` = every zone. */
  zones: string[] | null
  schedules: Schedule[]
  quiet_hours: QuietHours | null
  alert_when_almost_full: boolean
  /** Special spaces named in every notification and followed while on the way (P9.2). */
  space_types: SpaceType[]
}

export const DEFAULT_PREFS: Prefs = {
  proximity: false,
  radius_m: null,
  zones: null,
  schedules: [],
  quiet_hours: null,
  alert_when_almost_full: false,
  space_types: [],
}

export const MAX_SCHEDULES = 10
/** Settings are saved this long after the last change (one PATCH for a burst of taps). */
export const PREFS_SAVE_DELAY_MS = 500

/** Why an action failed, for the message under the buttons. */
export class PushError extends Error {
  readonly code: 'denied' | 'dismissed' | 'unavailable' | 'rate_limited' | 'gone' | 'failed'

  constructor(code: PushError['code'], message: string = code) {
    super(message)
    this.name = 'PushError'
    this.code = code
  }
}

export type PushSupport = 'supported' | 'unsupported' | 'ios-not-installed'

/**
 * iPhone Safari outside the installed app has no Web Push at all (notifications.md §2), so it gets
 * the install hint; any other browser without service worker + PushManager + Notification can't.
 */
export function pushSupport(win: Window = window): PushSupport {
  if (isIosSafari(win.navigator) && !isStandalone(win)) return 'ios-not-installed'
  const ok = 'serviceWorker' in win.navigator && 'PushManager' in win && 'Notification' in win
  return ok ? 'supported' : 'unsupported'
}

export function permission(): NotificationPermission {
  return typeof Notification === 'undefined' ? 'default' : Notification.permission
}

// --- local state: the endpoint (to address this subscription later) and the last saved prefs ---

const ENDPOINT = 'parking.pushEndpoint'
const PREFS = 'parking.pushPrefs'
const ON_MY_WAY = 'parking.onMyWayUntil'
export const MOCK_ENDPOINT = 'mock:local'

function getItem(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null // private mode
  }
}

function setItem(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    // private mode: works for this visit only
  }
}

export function storedEndpoint(): string | null {
  return getItem(ENDPOINT)
}

/** The prefs last saved on this device (defaults filled in), also used by Tier 1 (P6.5). */
export function loadPrefs(): Prefs {
  try {
    const raw: unknown = JSON.parse(getItem(PREFS) ?? 'null')
    if (typeof raw === 'object' && raw !== null && !Array.isArray(raw))
      return { ...DEFAULT_PREFS, ...(raw as Partial<Prefs>) }
  } catch {
    // corrupt: start over
  }
  return { ...DEFAULT_PREFS }
}

/** Fired on `window` when the saved prefs change (Tier 1 follows *Tell me when I'm near*). */
export const PREFS_EVENT = 'parking:prefs'

function remember(endpoint: string | null, prefs: Prefs | null) {
  setItem(ENDPOINT, endpoint)
  setItem(PREFS, prefs && JSON.stringify(prefs))
  window.dispatchEvent(new Event(PREFS_EVENT))
}

/** Prefs changed while push is off (Tier 1 only); sent to the server when push is enabled. */
export function saveLocalPrefs(prefs: Prefs) {
  remember(storedEndpoint(), prefs)
}

// --- API (api.md §2) ---

const isObject = (x: unknown): x is Record<string, unknown> => typeof x === 'object' && x !== null
const isKey = (x: unknown): x is { key: string } => isObject(x) && typeof x.key === 'string'
const isAny = (x: unknown): x is unknown => (void x, true)
const isOnMyWayResult = (x: unknown): x is { until: string | null; sent: boolean } =>
  isObject(x) && (typeof x.until === 'string' || x.until === null) && typeof x.sent === 'boolean'
const isTestResult = (x: unknown): x is { sent: boolean; deleted: boolean } =>
  isObject(x) && typeof x.sent === 'boolean' && typeof x.deleted === 'boolean'

function timeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

/** The API's error codes as PushErrors; anything else (network, 5xx) is `failed`. */
function asPushError(err: unknown): PushError {
  if (err instanceof PushError) return err
  const code = isObject(err) && isObject(err.error) ? err.error.code : undefined
  if (code === 'unavailable') return new PushError('unavailable')
  if (code === 'rate_limited') return new PushError('rate_limited')
  if (code === 'not_found') return new PushError('gone')
  return new PushError('failed', err instanceof Error ? err.message : String(err))
}

async function registration(): Promise<ServiceWorkerRegistration> {
  // `getRegistration` first: `ready` never settles where no service worker is registered (`vite dev`).
  if (!(await navigator.serviceWorker.getRegistration()))
    throw new PushError('failed', 'no service worker')
  return navigator.serviceWorker.ready
}

/** How long `pushManager.subscribe` may take: a first FCM registration has hung on Chromium. */
export const SUBSCRIBE_TIMEOUT_MS = 30_000

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new PushError('failed', `no answer in ${ms / 1000} s`)), ms)
  })
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer))
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.getRegistration()
  return reg ? reg.pushManager.getSubscription() : null
}

async function register(sub: PushSubscription, prefs: Prefs, base: string) {
  const reg = { prefs, tz: timeZone(), lang: i18n.language }
  await sendJson('POST', '/api/push/subscriptions', { subscription: sub.toJSON(), ...reg }, isAny, {
    base,
  })
  remember(sub.endpoint, prefs)
  await savePushRegistration(reg).catch(() => undefined) // for `pushsubscriptionchange`
}

/** True when this device has a subscription the app registered (and permission still holds). */
export async function isSubscribed(base = API_BASE): Promise<boolean> {
  const endpoint = storedEndpoint()
  if (!endpoint || permission() !== 'granted') return false
  if (base === 'mock') return endpoint === MOCK_ENDPOINT
  try {
    return (await currentSubscription())?.endpoint === endpoint
  } catch {
    return false
  }
}

/**
 * "Enable notifications". **Call straight from the click handler**: the permission prompt needs
 * the tap (iOS refuses it otherwise), so it is the first thing awaited.
 */
export async function subscribe(prefs: Prefs, base = API_BASE): Promise<void> {
  const answer = await Notification.requestPermission()
  if (answer === 'denied') throw new PushError('denied')
  if (answer !== 'granted') throw new PushError('dismissed')
  if (base === 'mock') {
    remember(MOCK_ENDPOINT, prefs)
    return
  }
  try {
    const reg = await registration()
    const { key } = await getRequest('/api/push/vapid-public-key', isKey, { base })
    const options = { userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) }
    let sub: PushSubscription
    try {
      sub = await withTimeout(reg.pushManager.subscribe(options), SUBSCRIBE_TIMEOUT_MS)
    } catch (err) {
      // Subscribed before with another key (the server's keys were replaced): start over.
      const old = await reg.pushManager.getSubscription()
      if (!old) throw err
      await old.unsubscribe()
      sub = await withTimeout(reg.pushManager.subscribe(options), SUBSCRIBE_TIMEOUT_MS)
    }
    await register(sub, prefs, base)
  } catch (err) {
    throw asPushError(err)
  }
}

/** Saves changed settings (`PATCH`); re-registers when the server no longer knows this device. */
export async function updatePrefs(prefs: Prefs, base = API_BASE): Promise<void> {
  const endpoint = storedEndpoint()
  if (!endpoint) throw new PushError('gone')
  if (base === 'mock') {
    remember(endpoint, prefs)
    return
  }
  try {
    await sendJson('PATCH', '/api/push/subscriptions', { endpoint, prefs }, isAny, { base })
    remember(endpoint, prefs)
    await savePushRegistration({ prefs, tz: timeZone(), lang: i18n.language }).catch(
      () => undefined,
    )
  } catch (err) {
    const e = asPushError(err)
    const sub = e.code === 'gone' ? await currentSubscription().catch(() => null) : null
    if (!sub) throw e
    await register(sub, prefs, base).catch((again: unknown) => {
      throw asPushError(again)
    })
  }
}

/** "Turn off": forget the subscription on the server and in the browser. Never fails. */
export async function unsubscribe(base = API_BASE): Promise<void> {
  const endpoint = storedEndpoint()
  remember(null, loadPrefs()) // the prefs stay: Tier 1 keeps working without push
  setItem(ON_MY_WAY, null)
  if (base === 'mock') return
  await clearPushRegistration().catch(() => undefined)
  if (endpoint)
    await sendJson('DELETE', '/api/push/subscriptions', { endpoint }, isAny, { base }).catch(
      () => undefined,
    )
  const sub = await currentSubscription().catch(() => null)
  await sub?.unsubscribe().catch(() => undefined)
}

/** The zones as in a push body (api.md §6): "Ground 12 · Underground ≈11", then the special
 * spaces this device follows (`prefs.space_types`) that the lot has: "Accessible 1". */
export function zonesSummary(status: LotStatus, types = loadPrefs().space_types): string {
  const zones = status.zones.map((z) => `${z.name} ${z.method === 'flow' ? '≈' : ''}${z.free}`)
  const special = specialTotals(status.zones)
    .filter((s) => types.includes(s.type))
    .map((s) => `${t(`special.${s.type}`)} ${s.free}`)
  return [...zones, ...special].join(' · ')
}

/** "You're 400 m away · 23 free (Ground 12 · Underground ≈11)" (notifications.md §3). */
export function proximityText(distanceM: number, status: LotStatus | null): string {
  const { unit, value } = roundDistance(distanceM)
  const away = t(`proximity.away_${unit}`, { value })
  if (!status) return away
  const free = t('proximity.free', { count: status.total.free, zones: zonesSummary(status) })
  return `${away} · ${free}`
}

/** The test notification's text in mock mode, shaped like the server's (api.md §6). */
export function mockTestBody(status: LotStatus): string {
  return t('alerts.mock_test_body', { count: status.total.free, zones: zonesSummary(status) })
}

/**
 * A notification shown by this device itself (Tier 1), looking like a push (api.md §6, same
 * tag). Does nothing without the notification permission or a service worker.
 */
export async function showLocalNotification(payload: {
  title: string
  body: string
  kind: string
}): Promise<void> {
  if (permission() !== 'granted' || !('serviceWorker' in navigator)) return
  const reg = await navigator.serviceWorker.getRegistration()
  if (!reg) return
  const { title, options } = notificationFromPush(JSON.stringify(payload), reg.scope)
  await reg.showNotification(title, options)
}

/**
 * "Send test notification": `POST /api/push/test` (3/hour per device). In mock mode the service
 * worker shows one locally. A subscription the push service has dropped (`deleted`) is forgotten
 * and reported as `gone`.
 */
export async function sendTest(base = API_BASE): Promise<void> {
  const endpoint = storedEndpoint()
  if (!endpoint) throw new PushError('gone')
  if (base === 'mock') {
    const reg = await registration()
    const payload = { title: t('alerts.mock_test_title'), body: mockTestBody(mockStatus()) }
    const { title, options } = notificationFromPush(JSON.stringify(payload), reg.scope)
    await reg.showNotification(title, { ...options, tag: STATUS_TAG })
    return
  }
  let result: { sent: boolean; deleted: boolean }
  try {
    result = await sendJson('POST', '/api/push/test', { endpoint }, isTestResult, { base })
  } catch (err) {
    throw asPushError(err)
  }
  if (result.deleted) {
    await unsubscribe(base)
    throw new PushError('gone')
  }
  if (!result.sent) throw new PushError('failed', 'push service refused')
}

// --- Tier 2: "I'm on my way" (notifications.md §4) ---

export const ON_MY_WAY_MINUTES = [15, 30, 60] as const

/** When this device's on-my-way window ends (`Date.now()` ms), or null when none is running. */
export function onMyWayUntil(now = Date.now()): number | null {
  const until = Number(getItem(ON_MY_WAY))
  return Number.isFinite(until) && until > now ? until : null
}

/**
 * "I'm on my way" for `minutes`: the server pushes the status now and then whenever it changes
 * enough, until the window ends, also with the app closed. Returns the end (`Date.now()` ms). In
 * mock mode the first notification is shown locally and nothing follows.
 */
export async function startOnMyWay(minutes: number, base = API_BASE): Promise<number> {
  const endpoint = storedEndpoint()
  if (!endpoint) throw new PushError('gone')
  let until: number
  if (base === 'mock') {
    until = Date.now() + minutes * 60_000
    const status = mockStatus()
    const payload = {
      title: t('alerts.on_my_way_title', { count: status.total.free }),
      body: zonesSummary(status),
      kind: 'on_my_way',
    }
    await showLocalNotification(payload).catch(() => undefined)
  } else {
    let result: { until: string | null; sent: boolean }
    try {
      const body = { endpoint, minutes }
      result = await sendJson('POST', '/api/push/on-my-way', body, isOnMyWayResult, { base })
    } catch (err) {
      throw asPushError(err)
    }
    until = result.until ? Date.parse(result.until) : Date.now() + minutes * 60_000
  }
  setItem(ON_MY_WAY, String(until))
  return until
}

/** Stops the on-my-way updates (`minutes: 0`). On a network error the window is kept. */
export async function cancelOnMyWay(base = API_BASE): Promise<void> {
  const endpoint = storedEndpoint()
  if (endpoint && base !== 'mock') {
    try {
      await sendJson('POST', '/api/push/on-my-way', { endpoint, minutes: 0 }, isAny, { base })
    } catch (err) {
      const e = asPushError(err)
      if (e.code !== 'gone') throw e // unknown to the server: nothing left to cancel
    }
  }
  setItem(ON_MY_WAY, null)
}

/** "14:59" left of a window, from milliseconds. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}
