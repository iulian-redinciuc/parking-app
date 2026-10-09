/// <reference lib="webworker" />
import { clientsClaim } from 'workbox-core'
import {
  cleanupOutdatedCaches,
  createHandlerBoundToURL,
  precacheAndRoute,
} from 'workbox-precaching'
import { NavigationRoute, registerRoute } from 'workbox-routing'
import { API_BASE } from './api/base'
import { API_PATH } from './lib/pwa'
import { notificationFromPush, openFromNotification, renewSubscription } from './lib/swPush'

declare let self: ServiceWorkerGlobalScope

// The service worker (frontend.md §5). The app shell is precached so it opens with no network.
// Nothing else is cached: there is no runtime caching at all, so `/api/*` (status, SSE, admin)
// always goes to the network and live numbers are never stale-from-cache. Web Push (P6.3): show
// the status push, open the app on a tap, renew a subscription the browser replaced.

self.skipWaiting() // registerType 'autoUpdate': a new version takes over at once
clientsClaim()

cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

// Any navigation inside the scope gets the cached shell (routes are in the hash), except the API.
registerRoute(
  new NavigationRoute(createHandlerBoundToURL('index.html'), {
    denylist: [API_PATH],
  }),
)

// A push must always show a notification (`userVisibleOnly`). Same tag → replaced quietly.
self.addEventListener('push', (event) => {
  const { title, options } = notificationFromPush(
    event.data?.text() ?? null,
    self.registration.scope,
  )
  event.waitUntil(self.registration.showNotification(title, options))
})

// Tap on a notification: focus an open app window (and move it to the target), else open one.
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    openFromNotification(self.clients, self.registration.scope, event.notification.data),
  )
})

// The browser expired or rotated the subscription: subscribe again and re-register it with the
// settings the page saved (lib/swPush.ts). Not in lib.webworker yet, hence the local type.
interface PushSubscriptionChangeEvent extends ExtendableEvent {
  readonly oldSubscription: PushSubscription | null
  readonly newSubscription: PushSubscription | null
}

self.addEventListener('pushsubscriptionchange', (e) => {
  const event = e as PushSubscriptionChangeEvent
  event.waitUntil(
    renewSubscription({
      pushManager: self.registration.pushManager,
      oldSubscription: event.oldSubscription ?? null,
      newSubscription: event.newSubscription ?? null,
      apiBase: API_BASE,
      fetch: (...args) => self.fetch(...args),
    }).catch((err: unknown) => console.warn('push subscription renewal failed', err)),
  )
})
