import { afterEach, describe, expect, it, vi } from 'vitest'
import errorBadRequest from './__fixtures__/error-bad-request.json'
import errorUnavailable from './__fixtures__/error-unavailable.json'
import lotInfo from './__fixtures__/lot-info.json'
import lotStatus from './__fixtures__/lot-status.json'
import { ApiRequestError, apiUrl, getLot, getStatus, IS_MOCK } from './client'
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
