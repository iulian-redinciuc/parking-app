// Web Push pieces shared by the service worker and the page (frontend.md §5, notifications.md §2).
// Kept free of DOM-only and app code so `sw.ts` can import it.
import { notificationUrl } from './pwa'

/** The payload's `tag` (api.md §6): a newer status replaces the shown one instead of stacking. */
export const STATUS_TAG = 'parking-status'

export interface PushNotification {
  title: string
  options: NotificationOptions & { renotify?: boolean }
}

function str(x: Record<string, unknown>, key: string): string | undefined {
  const value = x[key]
  return typeof value === 'string' && value ? value : undefined
}

/**
 * What to show for a push: the JSON payload of api.md §6, or any other text as the body (e.g.
 * DevTools' "Push" button sends plain text). `renotify: false`: an update to the same tag
 * replaces the notification quietly.
 */
export function notificationFromPush(text: string | null, scope: string): PushNotification {
  let payload: Record<string, unknown> = {}
  if (text) {
    try {
      const parsed: unknown = JSON.parse(text)
      payload =
        typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
          ? (parsed as Record<string, unknown>)
          : { body: text }
    } catch {
      payload = { body: text }
    }
  }
  return {
    title: str(payload, 'title') ?? 'Parking',
    options: {
      body: str(payload, 'body') ?? '',
      tag: str(payload, 'tag') ?? STATUS_TAG,
      renotify: false,
      icon: new URL('icons/192.png', scope).href,
      badge: new URL('icons/badge-96.png', scope).href,
      data: { url: str(payload, 'url'), kind: str(payload, 'kind'), level: str(payload, 'level') },
    },
  }
}

/**
 * A tap on a notification: focus an open app window (moving it to the payload's `url`), else open
 * one. Only same-origin URLs are followed (`notificationUrl`).
 */
export async function openFromNotification(
  clients: Pick<Clients, 'matchAll' | 'openWindow'>,
  scope: string,
  data: unknown,
): Promise<void> {
  const url = notificationUrl(data, scope)
  const windows = await clients.matchAll({ type: 'window', includeUncontrolled: true })
  const open = windows.find((c) => c.url.startsWith(scope)) as WindowClient | undefined
  if (open) {
    await open.focus()
    if (open.url !== url) await open.navigate(url).catch(() => undefined)
    return
  }
  await clients.openWindow(url)
}

/** A VAPID public key (unpadded base64url) as `applicationServerKey` bytes. */
export function urlBase64ToUint8Array(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = (base64url + '='.repeat((4 - (base64url.length % 4)) % 4))
    .replace(/-/g, '+')
    .replace(/_/g, '/')
  const raw = atob(base64)
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

/**
 * What the page last sent with `POST /api/push/subscriptions` besides the subscription, so the
 * service worker can re-register a renewed subscription with the same settings. Kept in Cache
 * Storage, the one store both the page and the service worker can reach without a library.
 */
export interface PushRegistration {
  prefs: unknown
  tz: string
  lang: string
}

const STORE = 'parking-push'
const KEY = '/__parking/push-registration' // root-relative: the same URL from the page and the SW

export async function savePushRegistration(reg: PushRegistration): Promise<void> {
  const cache = await caches.open(STORE)
  await cache.put(
    KEY,
    new Response(JSON.stringify(reg), { headers: { 'Content-Type': 'application/json' } }),
  )
}

export async function loadPushRegistration(): Promise<PushRegistration | null> {
  const res = await (await caches.open(STORE)).match(KEY)
  if (!res) return null
  try {
    const reg: unknown = await res.json()
    return typeof reg === 'object' && reg !== null && 'tz' in reg && 'lang' in reg
      ? (reg as PushRegistration)
      : null
  } catch {
    return null
  }
}

export async function clearPushRegistration(): Promise<void> {
  await caches.delete(STORE)
}

export interface RenewDeps {
  pushManager: Pick<PushManager, 'subscribe'>
  oldSubscription: PushSubscription | null
  newSubscription: PushSubscription | null
  /** `mock`, or the API origin. */
  apiBase: string
  fetch: typeof fetch
  load?: () => Promise<PushRegistration | null>
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * `pushsubscriptionchange`: the browser dropped or rotated the subscription. Subscribe again (the
 * browser may already hand us the new one), register it with the saved settings and forget the
 * old endpoint. Nothing to do in mock mode or when this device never enabled notifications.
 */
export async function renewSubscription({
  pushManager,
  oldSubscription,
  newSubscription,
  apiBase,
  fetch,
  load = loadPushRegistration,
}: RenewDeps): Promise<'renewed' | 'skipped'> {
  if (apiBase === 'mock') return 'skipped'
  const saved = await load()
  if (!saved) return 'skipped'

  let sub = newSubscription
  if (!sub) {
    let key: BufferSource | null = oldSubscription?.options.applicationServerKey ?? null
    if (!key) {
      const res = await fetch(`${apiBase}/api/push/vapid-public-key`)
      if (!res.ok) throw new Error(`vapid-public-key: HTTP ${res.status}`)
      key = urlBase64ToUint8Array(((await res.json()) as { key: string }).key)
    }
    sub = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
  }

  const res = await fetch(`${apiBase}/api/push/subscriptions`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      subscription: sub.toJSON(),
      prefs: saved.prefs,
      tz: saved.tz,
      lang: saved.lang,
    }),
  })
  if (!res.ok) throw new Error(`subscriptions: HTTP ${res.status}`)

  if (oldSubscription && oldSubscription.endpoint !== sub.endpoint)
    await fetch(`${apiBase}/api/push/subscriptions`, {
      method: 'DELETE',
      headers: JSON_HEADERS,
      body: JSON.stringify({ endpoint: oldSubscription.endpoint }),
    }).catch(() => undefined)
  return 'renewed'
}
