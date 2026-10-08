/// <reference lib="webworker" />
import { clientsClaim } from 'workbox-core'
import {
  cleanupOutdatedCaches,
  createHandlerBoundToURL,
  precacheAndRoute,
} from 'workbox-precaching'
import { NavigationRoute, registerRoute } from 'workbox-routing'
import { API_PATH, notificationUrl } from './lib/pwa'

declare let self: ServiceWorkerGlobalScope

// The service worker (frontend.md §5). The app shell is precached so it opens with no network.
// Nothing else is cached: there is no runtime caching at all, so `/api/*` (status, SSE, admin)
// always goes to the network and live numbers are never stale-from-cache. Push handlers come in
// Phase 6.

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

// Tap on a notification: focus an open app window (and move it to the target), else open one.
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = notificationUrl(event.notification.data, self.registration.scope)
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const open = windows.find((c) => c.url.startsWith(self.registration.scope))
      if (open) {
        await open.focus()
        if (open.url !== url) await open.navigate(url).catch(() => undefined)
        return
      }
      await self.clients.openWindow(url)
    })(),
  )
})
