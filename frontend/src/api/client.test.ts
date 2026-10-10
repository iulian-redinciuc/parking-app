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
  correctZone,
  getAdminAlerts,
  getAdminCameras,
  getForecast,
  getHistory,
  getAdminSession,
  getCameraConfig,
  getAdminToken,
  getCameraSnapshot,
  getCorrections,
  getLot,
  getStatus,
  IS_MOCK,
  onAdminTokenChange,
  putCameraConfig,
  saveReferenceFrame,
  setAdminAlerts,
  getSlotMap,
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

  it('adds lang to a path that already has a query', () => {
    expect(apiUrl('/api/admin/corrections?limit=5', 'ro', BASE)).toBe(
      'http://api.test/api/admin/corrections?limit=5&lang=ro',
    )
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

describe('slot map client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('answers the SVG text, null on 404, and fails on anything else', async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
    const fetchMock = vi.fn(async () => new Response(svg, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await getSlotMap('ground', { base: BASE })).toBe(svg)
    expect(fetchMock.mock.calls[0][0 as never]).toBe('http://api.test/api/maps/ground')

    vi.stubGlobal('fetch', reply(404, { error: { code: 'not_found', message: 'no map' } }))
    expect(await getSlotMap('ground', { base: BASE })).toBeNull()

    vi.stubGlobal('fetch', reply(503, {}))
    expect(await failure(getSlotMap('ground', { base: BASE }))).toMatchObject({
      code: 'bad_response',
      status: 503,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('failed to fetch')
      }),
    )
    expect((await failure(getSlotMap('ground', { base: BASE }))).code).toBe('network')
  })

  it('has a map for the mock lot', async () => {
    expect(await getSlotMap('ground')).toContain('<rect id="G01"')
    expect(await getSlotMap('underground')).toBeNull()
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

describe('admin cameras client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    setAdminToken(null)
  })

  const camera = {
    id: 'cam-ground',
    role: 'occupancy',
    zones: ['ground'],
    state: 'ok',
    issue: null,
    fps: 0.2,
    last_frame_age_s: 3.1,
    inference_ms_avg: 151,
    unhealthy_ratio: 0,
    last_health_age_s: 1,
    snapshot: true,
  }

  it('lists cameras with the bearer token and checks the shape', async () => {
    setAdminToken('tok')
    const fetch = reply(200, [camera])
    vi.stubGlobal('fetch', fetch)
    expect(await getAdminCameras({ base: BASE })).toEqual([camera])
    const init = (fetch.mock.calls[0] as unknown[])[1] as RequestInit
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' })

    vi.stubGlobal('fetch', reply(200, [{ ...camera, state: 'great' }]))
    expect((await failure(getAdminCameras({ base: BASE }))).code).toBe('bad_response')
  })

  it('fetches the snapshot as a blob with the bearer header, never cached', async () => {
    setAdminToken('tok')
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9])
    const fetch = vi.fn(
      async () => new Response(jpeg, { status: 200, headers: { 'Content-Type': 'image/jpeg' } }),
    )
    vi.stubGlobal('fetch', fetch)
    const blob = await getCameraSnapshot('cam-ground', { base: BASE, annotated: false })
    expect(blob.type).toBe('image/jpeg')
    expect(blob.size).toBe(4)
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://api.test/api/admin/cameras/cam-ground/snapshot?annotated=false')
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' })
    expect(init.cache).toBe('no-store')
  })

  it('a 503 snapshot is `unavailable` and keeps the token; a 401 forgets it', async () => {
    setAdminToken('tok')
    vi.stubGlobal('fetch', reply(503, { error: { code: 'unavailable', message: 'no frame' } }))
    const error = await failure(getCameraSnapshot('cam-ground', { base: BASE }))
    expect(error).toMatchObject({ code: 'unavailable', status: 503 })
    expect(getAdminToken()).toBe('tok')

    vi.stubGlobal('fetch', reply(401, { error: { code: 'unauthorized', message: 'expired' } }))
    expect((await failure(getCameraSnapshot('cam-ground', { base: BASE }))).status).toBe(401)
    expect(getAdminToken()).toBeNull()
    expect((await failure(getCameraSnapshot('cam-ground', { base: BASE }))).status).toBe(401)
  })

  it('a network failure is `network`', async () => {
    setAdminToken('tok')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('offline'))),
    )
    expect((await failure(getCameraSnapshot('cam-ground', { base: BASE }))).code).toBe('network')
  })
})

describe('admin slot/line editor client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    setAdminToken(null)
  })

  const file = { version: 1, camera_id: 'cam-ground', image_size: [640, 360], slots: [] }

  it('GETs and PUTs the slot file with the bearer token', async () => {
    setAdminToken('tok')
    const get = reply(200, file)
    vi.stubGlobal('fetch', get)
    expect(await getCameraConfig('cam-ground', 'slots', { base: BASE })).toEqual(file)
    expect((get.mock.calls[0] as unknown[])[0]).toBe(
      'http://api.test/api/admin/cameras/cam-ground/slots',
    )

    const saved = {
      saved: 'config/slots/cam-ground.json',
      backup: true,
      reloaded: true,
      message: null,
      slots: 0,
    }
    const put = reply(200, saved)
    vi.stubGlobal('fetch', put)
    expect(await putCameraConfig('cam-ground', 'slots', file, { base: BASE })).toEqual(saved)
    const [url, init] = put.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://api.test/api/admin/cameras/cam-ground/slots')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body as string)).toEqual(file)
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' })
  })

  it('a rejected file is `bad_request` with the reason; a missing file is 404', async () => {
    setAdminToken('tok')
    vi.stubGlobal(
      'fetch',
      reply(422, { error: { code: 'bad_request', message: 'slots.0.polygon: self-intersecting' } }),
    )
    const error = await failure(putCameraConfig('cam-ground', 'slots', file, { base: BASE }))
    expect(error).toMatchObject({
      code: 'bad_request',
      message: 'slots.0.polygon: self-intersecting',
    })

    vi.stubGlobal(
      'fetch',
      reply(404, { error: { code: 'not_found', message: 'no lines file yet' } }),
    )
    expect((await failure(getCameraConfig('cam-ramp', 'lines', { base: BASE }))).status).toBe(404)
  })

  it('saves the reference frame; 409 before the first frame', async () => {
    setAdminToken('tok')
    const fetch = reply(200, { saved: 'data/reference/cam-ground.jpg', ts: '2026-10-09T12:00:00Z' })
    vi.stubGlobal('fetch', fetch)
    expect((await saveReferenceFrame('cam-ground', { base: BASE })).saved).toBe(
      'data/reference/cam-ground.jpg',
    )
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://api.test/api/admin/cameras/cam-ground/reference-frame')
    expect(init.method).toBe('POST')

    vi.stubGlobal(
      'fetch',
      reply(409, { error: { code: 'conflict', message: 'no frame read yet' } }),
    )
    expect((await failure(saveReferenceFrame('cam-ground', { base: BASE }))).code).toBe('conflict')
  })
})

describe('admin corrections client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    setAdminToken(null)
  })

  const zone = lotStatus.zones.find((z) => z.method === 'flow')!
  const entry = {
    id: 3,
    ts: '2026-10-09T12:01:00.000Z',
    zone_id: zone.id,
    zone_name: zone.name,
    old_occupied: 5,
    new_occupied: 37,
    actor: 'session:2',
    note: 'manual count',
  }

  it('POSTs the count and the trimmed note with the bearer token', async () => {
    setAdminToken('tok')
    const fetch = reply(200, { ...zone, occupied: 37 })
    vi.stubGlobal('fetch', fetch)
    const result = await correctZone(zone, 37, ' manual count ', { base: BASE })
    expect(result.occupied).toBe(37)
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`http://api.test/api/admin/zones/${zone.id}/correct`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ occupied: 37, note: 'manual count' })
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' })
  })

  it('a zone that is not a flow zone is a 409 `conflict`', async () => {
    setAdminToken('tok')
    vi.stubGlobal('fetch', reply(409, { error: { code: 'conflict', message: 'not flow' } }))
    expect(await failure(correctZone(zone, 1, '', { base: BASE }))).toMatchObject({
      code: 'conflict',
      status: 409,
    })
    expect(getAdminToken()).toBe('tok')
  })

  it('reads the log with a limit and checks the shape', async () => {
    setAdminToken('tok')
    const fetch = reply(200, [entry])
    vi.stubGlobal('fetch', fetch)
    expect(await getCorrections(50, { base: BASE })).toEqual([entry])
    expect((fetch.mock.calls[0] as unknown[])[0]).toBe(
      'http://api.test/api/admin/corrections?limit=50',
    )
    vi.stubGlobal('fetch', reply(200, [{ ...entry, old_occupied: '5' }]))
    expect(await failure(getCorrections(50, { base: BASE }))).toMatchObject({
      code: 'bad_response',
    })
  })
})

describe('history and forecast (real API)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('sends the query and validates the answer', async () => {
    const body = {
      zone: 'total',
      bucket: 'minute',
      from: '2026-10-09T09:00:00Z',
      to: '2026-10-09T10:00:00Z',
      points: [
        {
          t: '2026-10-09T09:00:00Z',
          free_avg: 12.5,
          free_min: 12,
          free_max: 13,
          occupied_avg: 87.5,
        },
      ],
    }
    const fetch = reply(200, body)
    vi.stubGlobal('fetch', fetch)
    const history = await getHistory(
      { zone: 'total', from: body.from, bucket: 'minute' },
      { base: BASE },
    )
    expect(history.points[0].free_avg).toBe(12.5)
    expect(fetch).toHaveBeenCalledWith(
      'http://api.test/api/history?zone=total&from=2026-10-09T09%3A00%3A00Z&bucket=minute',
      expect.anything(),
    )
    vi.stubGlobal('fetch', reply(200, { ...body, points: [{ t: 'x' }] }))
    expect((await failure(getHistory({}, { base: BASE }))).code).toBe('bad_response')
  })

  it('passes not_enough_data through as a 404', async () => {
    vi.stubGlobal(
      'fetch',
      reply(404, { error: { code: 'not_enough_data', message: 'fewer than 3 weeks' } }),
    )
    const err = await failure(getForecast({ zone: 'ground' }, { base: BASE }))
    expect(err).toMatchObject({ code: 'not_enough_data', status: 404 })
    vi.stubGlobal(
      'fetch',
      reply(200, {
        zone: 'ground',
        at: '2026-10-09T10:30:00Z',
        free_expected: 9,
        basis: 'x',
        samples: 6,
      }),
    )
    expect((await getForecast({}, { base: BASE })).free_expected).toBe(9)
  })
})

describe('admin alerts (real API)', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    setAdminToken(null)
  })

  it('reads the flag for this endpoint and sets it with a PUT', async () => {
    setAdminToken('tok')
    const body = { enabled: true, available: true, issues: [] }
    let fetch = reply(200, body)
    vi.stubGlobal('fetch', fetch)
    expect(await getAdminAlerts('https://push.test/a b', { base: BASE })).toEqual(body)
    expect((fetch.mock.calls[0] as unknown[])[0]).toBe(
      'http://api.test/api/admin/alerts?endpoint=https%3A%2F%2Fpush.test%2Fa%20b',
    )
    fetch = reply(200, { enabled: false })
    vi.stubGlobal('fetch', fetch)
    expect(await setAdminAlerts('https://push.test/a', false, { base: BASE })).toBe(false)
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://api.test/api/admin/alerts')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body as string)).toEqual({
      endpoint: 'https://push.test/a',
      enabled: false,
    })
  })

  it('an unknown subscription is a 404', async () => {
    setAdminToken('tok')
    vi.stubGlobal('fetch', reply(404, { error: { code: 'not_found', message: 'unknown' } }))
    expect(
      await failure(setAdminAlerts('https://push.test/x', true, { base: BASE })),
    ).toMatchObject({ status: 404 })
  })
})
