import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import liveJson from './__fixtures__/lot-status.json'
import { ApiRequestError } from './client'
import { POLL_MS, SSE_RETRY_MS, WATCHDOG_MS, createLiveFeed, type EventSourceLike } from './live'
import type { LiveFeed, LotStatus } from './types'

const LIVE = liveJson as LotStatus
const withFree = (free: number): LotStatus => ({ ...LIVE, total: { ...LIVE.total, free } })

/** A controllable stand-in for the browser's `EventSource`. */
class FakeEventSource implements EventSourceLike {
  static all: FakeEventSource[] = []
  readyState = 0
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  closed = false
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>()

  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeEventSource.all.push(this)
  }

  static get last(): FakeEventSource {
    return FakeEventSource.all[FakeEventSource.all.length - 1]
  }

  static get open(): FakeEventSource[] {
    return FakeEventSource.all.filter((s) => !s.closed)
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  close() {
    this.closed = true
    this.readyState = 2
  }

  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  emit(type: string, data: string) {
    const event = new MessageEvent(type, { data })
    this.listeners.get(type)?.forEach((l) => l(event))
  }

  status(status: LotStatus) {
    this.emit('status', JSON.stringify(status))
  }

  fail(readyState: number) {
    this.readyState = readyState
    this.onerror?.(new Event('error'))
  }
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

function setOnline(online: boolean) {
  Object.defineProperty(navigator, 'onLine', { value: online, configurable: true })
  window.dispatchEvent(new Event(online ? 'online' : 'offline'))
}

describe('createLiveFeed', () => {
  let fetchStatus: ReturnType<typeof vi.fn<(signal: AbortSignal) => Promise<LotStatus>>>
  let feed: LiveFeed

  function make() {
    feed = createLiveFeed({
      base: 'http://api.test',
      lang: 'ro',
      eventSource: (url) => new FakeEventSource(url),
      fetchStatus,
    })
    return feed
  }

  beforeEach(() => {
    vi.useFakeTimers()
    FakeEventSource.all = []
    fetchStatus = vi.fn(() => Promise.resolve(withFree(10)))
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true })
  })

  afterEach(() => {
    feed?.stop()
    vi.useRealTimers()
  })

  it('fetches /api/status first, then goes live on the stream', async () => {
    make().start()
    expect(feed.getSnapshot().connection).toBe('connecting')
    expect(fetchStatus).toHaveBeenCalledTimes(1)
    expect(FakeEventSource.all).toHaveLength(1)
    expect(FakeEventSource.last.url).toBe('http://api.test/api/stream?lang=ro')

    await vi.advanceTimersByTimeAsync(0)
    expect(feed.getSnapshot().status?.total.free).toBe(10) // first paint before the stream
    expect(feed.getSnapshot().lastMessageAt).toBe(Date.now())

    FakeEventSource.last.open()
    FakeEventSource.last.status(withFree(7))
    expect(feed.getSnapshot()).toMatchObject({ connection: 'live', error: null })
    expect(feed.getSnapshot().status?.total.free).toBe(7)
  })

  it('notifies subscribers with a new snapshot object', () => {
    make()
    const listener = vi.fn()
    const unsubscribe = feed.subscribe(listener)
    const before = feed.getSnapshot()
    feed.start()
    FakeEventSource.last.status(withFree(3))
    expect(listener).toHaveBeenCalled()
    expect(feed.getSnapshot()).not.toBe(before)
    unsubscribe()
    listener.mockClear()
    FakeEventSource.last.status(withFree(4))
    expect(listener).not.toHaveBeenCalled()
  })

  it('live → silent 30 s → polling → SSE back → live', async () => {
    make().start()
    const first = FakeEventSource.last
    first.open()
    first.status(withFree(10))
    await vi.advanceTimersByTimeAsync(0)

    // pings keep it live although nothing changes
    await vi.advanceTimersByTimeAsync(WATCHDOG_MS - 1000)
    first.emit('ping', '')
    await vi.advanceTimersByTimeAsync(WATCHDOG_MS - 1000)
    expect(feed.getSnapshot().connection).toBe('live')
    expect(first.closed).toBe(false)

    // then silence: the stream is closed and /api/status polled every 10 s
    fetchStatus.mockClear()
    fetchStatus.mockResolvedValue(withFree(5))
    await vi.advanceTimersByTimeAsync(1000)
    expect(first.closed).toBe(true)
    expect(feed.getSnapshot().connection).toBe('polling')
    expect(fetchStatus).toHaveBeenCalledTimes(1)
    expect(feed.getSnapshot().status?.total.free).toBe(5)
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(fetchStatus).toHaveBeenCalledTimes(2)
    expect(FakeEventSource.open).toHaveLength(0)

    // SSE is tried again every 60 s; a retry that stays silent is dropped, polling goes on
    await vi.advanceTimersByTimeAsync(SSE_RETRY_MS - POLL_MS)
    expect(FakeEventSource.all).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(WATCHDOG_MS)
    expect(FakeEventSource.all[1].closed).toBe(true)
    expect(feed.getSnapshot().connection).toBe('polling')

    // the next retry gets an answer: live again, polling stops
    await vi.advanceTimersByTimeAsync(SSE_RETRY_MS - WATCHDOG_MS)
    expect(FakeEventSource.all).toHaveLength(3)
    FakeEventSource.last.open()
    FakeEventSource.last.status(withFree(2))
    expect(feed.getSnapshot().connection).toBe('live')
    expect(feed.getSnapshot().status?.total.free).toBe(2)
    fetchStatus.mockClear()
    for (let i = 0; i < 4; i++) {
      await vi.advanceTimersByTimeAsync(15_000) // sse_ping_s
      FakeEventSource.last.emit('ping', '')
    }
    expect(fetchStatus).not.toHaveBeenCalled()
    expect(FakeEventSource.all).toHaveLength(3)
  })

  it('falls back at once when the stream gives up (CLOSED), not on a normal reconnect', async () => {
    make().start()
    FakeEventSource.last.open()
    FakeEventSource.last.fail(0) // CONNECTING: the browser retries by itself
    expect(feed.getSnapshot().connection).toBe('live')
    FakeEventSource.last.fail(2)
    expect(feed.getSnapshot().connection).toBe('polling')
  })

  it('hidden → stream closed and no polling; visible → refetch and reopen', async () => {
    make().start()
    FakeEventSource.last.open()
    await vi.advanceTimersByTimeAsync(0)

    setVisibility('hidden')
    expect(FakeEventSource.last.closed).toBe(true)
    fetchStatus.mockClear()
    await vi.advanceTimersByTimeAsync(5 * SSE_RETRY_MS)
    expect(fetchStatus).not.toHaveBeenCalled()
    expect(FakeEventSource.all).toHaveLength(1)

    fetchStatus.mockResolvedValue(withFree(1))
    setVisibility('visible')
    expect(fetchStatus).toHaveBeenCalledTimes(1)
    expect(FakeEventSource.all).toHaveLength(2)
    expect(FakeEventSource.last.closed).toBe(false)
    await vi.advanceTimersByTimeAsync(0)
    expect(feed.getSnapshot().status?.total.free).toBe(1)
  })

  it('offline → offline and closed; online → reconnects', async () => {
    make().start()
    FakeEventSource.last.open()
    setOnline(false)
    expect(feed.getSnapshot().connection).toBe('offline')
    expect(FakeEventSource.open).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(2 * WATCHDOG_MS)
    expect(feed.getSnapshot().connection).toBe('offline')

    setOnline(true)
    expect(feed.getSnapshot().connection).toBe('connecting')
    expect(FakeEventSource.open).toHaveLength(1)
    FakeEventSource.last.open()
    expect(feed.getSnapshot().connection).toBe('live')
  })

  it('starts offline when the browser is offline', () => {
    Object.defineProperty(navigator, 'onLine', { value: false, configurable: true })
    make().start()
    expect(feed.getSnapshot().connection).toBe('offline')
    expect(FakeEventSource.all).toHaveLength(0)
  })

  it('keeps 503 unavailable as an error without a status', async () => {
    fetchStatus.mockRejectedValue(
      new ApiRequestError({ code: 'unavailable', message: 'no data', status: 503 }),
    )
    make().start()
    await vi.advanceTimersByTimeAsync(0)
    expect(feed.getSnapshot()).toMatchObject({
      status: null,
      connection: 'connecting',
      error: { code: 'unavailable' },
    })
    FakeEventSource.last.open() // the stream is up, data will come with the first status
    expect(feed.getSnapshot().connection).toBe('live')
  })

  it('a failed poll shows error and keeps the last status; the next good poll recovers', async () => {
    make().start()
    await vi.advanceTimersByTimeAsync(0)
    fetchStatus.mockRejectedValue(new ApiRequestError({ code: 'network', message: 'down' }))
    await vi.advanceTimersByTimeAsync(WATCHDOG_MS)
    expect(feed.getSnapshot()).toMatchObject({ connection: 'error', error: { code: 'network' } })
    expect(feed.getSnapshot().status?.total.free).toBe(10)

    fetchStatus.mockResolvedValue(withFree(4))
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(feed.getSnapshot()).toMatchObject({ connection: 'polling', error: null })
    expect(feed.getSnapshot().status?.total.free).toBe(4)
  })

  it('ignores a malformed status event', () => {
    make().start()
    FakeEventSource.last.open()
    FakeEventSource.last.status(withFree(6))
    FakeEventSource.last.emit('status', '{"v":1}')
    FakeEventSource.last.emit('status', 'not json')
    expect(feed.getSnapshot().status?.total.free).toBe(6)
    expect(feed.getSnapshot().error?.code).toBe('bad_response')
  })

  it('drops a late answer after hide, and stop() removes everything', async () => {
    let resolve: (s: LotStatus) => void = () => {}
    fetchStatus.mockReturnValue(new Promise((r) => (resolve = r)))
    make().start()
    setVisibility('hidden')
    resolve(withFree(9))
    await vi.advanceTimersByTimeAsync(0)
    expect(feed.getSnapshot().status).toBeNull()
    expect(fetchStatus.mock.calls[0][0].aborted).toBe(true)

    setVisibility('visible')
    feed.stop()
    expect(FakeEventSource.open).toHaveLength(0)
    setVisibility('visible')
    setOnline(true)
    await vi.advanceTimersByTimeAsync(5 * SSE_RETRY_MS)
    expect(FakeEventSource.all).toHaveLength(2)
  })

  it('start() twice opens one connection', () => {
    make().start()
    feed.start()
    expect(FakeEventSource.all).toHaveLength(1)
    expect(fetchStatus).toHaveBeenCalledTimes(1)
  })
})
