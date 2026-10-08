// The live connection (docs/design/frontend.md §3): one `GET /api/status` for a fast first
// paint, then `EventSource` on `/api/stream`. A 30 s watchdog falls back to polling every 10 s
// while SSE is retried every 60 s; hidden tabs close the stream, `online`/`offline` are followed.
import { API_BASE, ApiRequestError, IS_MOCK, apiUrl, getStatus } from './client'
import { createMockFeed } from './mock'
import type { ApiError, Connection, LiveFeed, LiveState, LotStatus } from './types'
import { isLotStatus } from './validate'

export const WATCHDOG_MS = 30_000
export const POLL_MS = 10_000
export const SSE_RETRY_MS = 60_000

/** The parts of `EventSource` we use, so tests can pass a fake. */
export interface EventSourceLike {
  readonly readyState: number
  onopen: ((event: Event) => void) | null
  onerror: ((event: Event) => void) | null
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export type EventSourceFactory = (url: string) => EventSourceLike

export interface LiveOptions {
  /** API origin; defaults to `API_BASE`. */
  base?: string
  /** Zone names in this language (`?lang=`). */
  lang?: string
  watchdogMs?: number
  pollMs?: number
  sseRetryMs?: number
  /** Defaults to `new EventSource(url)`. */
  eventSource?: EventSourceFactory
  /** Defaults to `getStatus()` from client.ts. */
  fetchStatus?: (signal: AbortSignal) => Promise<LotStatus>
  now?: () => number
}

const CLOSED = 2 // EventSource.CLOSED

const isOnline = () => typeof navigator === 'undefined' || navigator.onLine !== false
const isHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiRequestError) return err.error
  return { code: 'network', message: String(err) }
}

export function createLiveFeed(options: LiveOptions = {}): LiveFeed {
  const {
    base = API_BASE,
    lang,
    watchdogMs = WATCHDOG_MS,
    pollMs = POLL_MS,
    sseRetryMs = SSE_RETRY_MS,
    eventSource = (url) => new EventSource(url),
    fetchStatus = (signal) => getStatus({ base, lang, signal }),
    now = Date.now,
  } = options
  const listeners = new Set<() => void>()
  let state: LiveState = {
    status: null,
    connection: 'connecting',
    lastMessageAt: null,
    error: null,
  }
  let started = false
  let es: EventSourceLike | null = null
  let watchdog: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let sseRetryTimer: ReturnType<typeof setInterval> | null = null
  // aborted on every teardown, so a late answer never lands after hide/offline/stop
  let requests = new AbortController()

  function set(patch: Partial<LiveState>) {
    state = { ...state, ...patch }
    listeners.forEach((l) => l())
  }

  const polling = () => pollTimer !== null

  async function fetchOnce() {
    const { signal } = requests
    try {
      const status = await fetchStatus(signal)
      if (signal.aborted) return
      const connection: Connection =
        state.connection === 'error' ? (polling() ? 'polling' : 'connecting') : state.connection
      set({ status, lastMessageAt: now(), error: null, connection })
    } catch (err) {
      if (signal.aborted) return
      const error = toApiError(err)
      // `unavailable` (503) is an answer: the server is up but has no data yet
      if (error.code === 'unavailable') {
        set({ status: null, error })
      } else {
        // keep the last status on screen; the banner (P3.5) explains why it's old
        set({ error, connection: state.connection === 'live' ? 'live' : 'error' })
      }
    }
  }

  function closeSse() {
    if (watchdog !== null) clearTimeout(watchdog)
    watchdog = null
    if (es) {
      es.onopen = null
      es.onerror = null
      es.close()
    }
    es = null
  }

  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer)
    if (sseRetryTimer !== null) clearInterval(sseRetryTimer)
    pollTimer = null
    sseRetryTimer = null
  }

  function armWatchdog() {
    if (watchdog !== null) clearTimeout(watchdog)
    watchdog = setTimeout(onSilence, watchdogMs)
  }

  /** Any sign of life from the stream (open, status, ping): it's live again. */
  function alive(patch: Partial<LiveState> = {}) {
    armWatchdog()
    stopPolling()
    set({ connection: 'live', ...patch })
  }

  function openSse() {
    closeSse()
    const source = eventSource(apiUrl('/api/stream', lang, base))
    es = source
    source.onopen = () => alive()
    source.addEventListener('ping', () => {
      if (es === source) alive({ lastMessageAt: now() })
    })
    source.addEventListener('status', (event) => {
      if (es !== source) return
      let status: unknown
      try {
        status = JSON.parse(event.data)
      } catch {
        status = undefined
      }
      if (!isLotStatus(status)) {
        set({ error: { code: 'bad_response', message: 'unexpected status event' } })
        return
      }
      alive({ status, lastMessageAt: now(), error: null })
    })
    // EventSource reconnects by itself (`retry: 3000`); only a CLOSED source (e.g. a 503
    // instead of a stream) gives up, and then there is no point waiting for the watchdog
    source.onerror = () => {
      if (es === source && source.readyState === CLOSED) onSilence()
    }
    armWatchdog()
  }

  /** No message for `watchdogMs` (or the stream gave up): poll, and try SSE again later. */
  function onSilence() {
    closeSse()
    if (polling()) return // a retry that didn't come back: keep polling
    set({ connection: 'polling' })
    pollTimer = setInterval(fetchOnce, pollMs)
    sseRetryTimer = setInterval(openSse, sseRetryMs)
    void fetchOnce()
  }

  function teardown() {
    closeSse()
    stopPolling()
    requests.abort()
    requests = new AbortController()
  }

  function connect() {
    teardown()
    if (!isOnline()) {
      set({ connection: 'offline' })
      return
    }
    if (isHidden()) return
    if (state.connection !== 'live') set({ connection: 'connecting' })
    void fetchOnce()
    openSse()
  }

  function onVisibility() {
    if (isHidden()) teardown()
    else connect()
  }

  const onOnline = () => connect()
  function onOffline() {
    teardown()
    set({ connection: 'offline' })
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    start() {
      if (started) return
      started = true
      document.addEventListener('visibilitychange', onVisibility)
      window.addEventListener('online', onOnline)
      window.addEventListener('offline', onOffline)
      connect()
    },
    stop() {
      if (!started) return
      started = false
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
      teardown()
    },
  }
}

let shared: LiveFeed | null = null

/** The app's one connection: the mock feed when `VITE_API_BASE` is `mock` (or unset). */
export function liveFeed(): LiveFeed {
  shared ??= IS_MOCK ? createMockFeed() : createLiveFeed()
  return shared
}
