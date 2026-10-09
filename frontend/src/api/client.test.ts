import { afterEach, describe, expect, it, vi } from 'vitest'
import errorBadRequest from './__fixtures__/error-bad-request.json'
import errorUnavailable from './__fixtures__/error-unavailable.json'
import lotInfo from './__fixtures__/lot-info.json'
import lotStatus from './__fixtures__/lot-status.json'
import {
  adminLogin,
  adminLogout,
  ApiRequestError,
  apiUrl,
  getAdminSession,
  getAdminToken,
  getLot,
  getStatus,
  IS_MOCK,
  onAdminTokenChange,
  setAdminToken,
} from './client'
import type { ApiError } from './types'

const BASE = 'http://api.test'

function reply(status: number, body: unknown, headers: Record<string, string> = {}) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status, headers }))
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const err = await promise.then(
    () => null,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(ApiRequestError)
  return (err as ApiRequestError).error
}

describe('API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('defaults to mock mode (no VITE_API_BASE in tests)', async () => {
    expect(IS_MOCK).toBe(true)
    expect((await getStatus()).v).toBe(1)
    expect((await getLot()).id).toBe('main')
  })

  it('builds URLs with ?lang=', () => {
    expect(apiUrl('/api/status', undefined, BASE)).toBe('http://api.test/api/status')
    expect(apiUrl('/api/lot', 'ro', BASE)).toBe('http://api.test/api/lot?lang=ro')
  })

  it('returns a valid status and lot', async () => {
    const fetch = reply(200, lotStatus)
    vi.stubGlobal('fetch', fetch)
    expect(await getStatus({ base: BASE, lang: 'en' })).toEqual(lotStatus)
    expect(fetch).toHaveBeenCalledWith('http://api.test/api/status?lang=en', expect.anything())
    vi.stubGlobal('fetch', reply(200, lotInfo))
    expect(await getLot({ base: BASE })).toEqual(lotInfo)
  })

  it('parses the error format', async () => {
    vi.stubGlobal('fetch', reply(503, errorUnavailable))
    expect(await failure(getStatus({ base: BASE }))).toEqual({
      code: 'unavailable',
      message: 'no data received yet since start-up',
      status: 503,
      retryAfter: undefined,
    })
    vi.stubGlobal('fetch', reply(422, errorBadRequest))
    const err = await failure(getStatus({ base: BASE, lang: 'x'.repeat(36) }))
    expect(err.code).toBe('bad_request')
    expect(err.details?.[0].loc).toEqual(['query', 'lang'])
    vi.stubGlobal(
      'fetch',
      reply(
        429,
        { error: { code: 'rate_limited', message: 'slow down' } },
        { 'Retry-After': '12' },
      ),
    )
    expect(await failure(getLot({ base: BASE }))).toMatchObject({
      code: 'rate_limited',
      retryAfter: 12,
    })
  })

  it('rejects bodies that are not the contract', async () => {
    vi.stubGlobal('fetch', reply(200, { ...lotStatus, zones: 'none' }))
    expect(await failure(getStatus({ base: BASE }))).toMatchObject({
      code: 'bad_response',
      status: 200,
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<html>Bad Gateway</html>', { status: 502 })),
    )
    expect(await failure(getStatus({ base: BASE }))).toMatchObject({
      code: 'bad_response',
      status: 502,
    })
  })

  it('reports network errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))),
    )
    expect(await failure(getStatus({ base: BASE }))).toMatchObject({ code: 'network' })
  })

  it('times out after 10 s', async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_, reject) =>
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            ),
          ),
      ),
    )
    const result = failure(getStatus({ base: BASE }))
    await vi.advanceTimersByTimeAsync(9_999)
    await vi.advanceTimersByTimeAsync(1)
    expect(await result).toEqual({ code: 'timeout', message: 'no answer in 10 s' })
  })

  it("passes the caller's abort through", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_, reject) =>
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            ),
          ),
      ),
    )
    const controller = new AbortController()
    const result = getStatus({ base: BASE, signal: controller.signal })
    controller.abort()
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
  })
})

describe('admin client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    setAdminToken(null)
  })

  it('logs in, stores the token in sessionStorage and sends it as a bearer token', async () => {
    vi.stubGlobal('fetch', reply(200, { token: 'tok-1', expires_at: '2026-10-16T12:00:00.000Z' }))
    await adminLogin('pw', { base: BASE })
    expect(getAdminToken()).toBe('tok-1')
    expect(sessionStorage.getItem('parking.adminToken')).toBe('tok-1')

    const fetch = reply(200, { actor: 'session:1', expires_at: '2026-10-16T12:00:00.000Z' })
    vi.stubGlobal('fetch', fetch)
    expect((await getAdminSession({ base: BASE })).actor).toBe('session:1')
    const init = (fetch.mock.calls[0] as unknown[])[1] as RequestInit
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok-1' })
  })

  it('a wrong password is a 401 and stores nothing', async () => {
    vi.stubGlobal('fetch', reply(401, { error: { code: 'unauthorized', message: 'wrong' } }))
    expect((await failure(adminLogin('bad', { base: BASE }))).status).toBe(401)
    expect(getAdminToken()).toBeNull()
  })

  it('a 401 on an admin call forgets the token and tells listeners', async () => {
    setAdminToken('tok-old')
    const listener = vi.fn()
    const off = onAdminTokenChange(listener)
    vi.stubGlobal('fetch', reply(401, { error: { code: 'unauthorized', message: 'expired' } }))
    expect((await failure(getAdminSession({ base: BASE }))).code).toBe('unauthorized')
    expect(getAdminToken()).toBeNull()
    expect(listener).toHaveBeenCalled()
    off()
  })

  it('other errors keep the token', async () => {
    setAdminToken('tok-2')
    vi.stubGlobal('fetch', reply(503, { error: { code: 'unavailable', message: 'down' } }))
    await failure(getAdminSession({ base: BASE }))
    expect(getAdminToken()).toBe('tok-2')
  })

  it('logout revokes on the server and forgets the token even if that fails', async () => {
    setAdminToken('tok-3')
    const fetch = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetch)
    await adminLogout({ base: BASE })
    expect(fetch).toHaveBeenCalledWith('http://api.test/api/admin/logout', expect.anything())
    expect(getAdminToken()).toBeNull()

    setAdminToken('tok-4')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('offline'))),
    )
    await failure(adminLogout({ base: BASE }))
    expect(getAdminToken()).toBeNull()
  })
})
