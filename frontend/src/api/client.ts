// REST client for the API (docs/design/api.md §2, §4). Every failure becomes an
// `ApiRequestError` carrying an `ApiError`; a caller's own abort is passed through unchanged.
// Admin calls add `Authorization: Bearer <token>` from sessionStorage; a 401 forgets the token,
// which sends the admin screens back to the login.
import { API_BASE, IS_MOCK } from './base'
import { correctMockZone, mockLotInfo, mockStatus } from './mock'
import type { ApiError, LotInfo, LotStatus, ZoneStatus } from './types'
import { isApiErrorBody, isLotInfo, isLotStatus } from './validate'

export { API_BASE, IS_MOCK } from './base'
export const TIMEOUT_MS = 10_000

export class ApiRequestError extends Error {
  readonly error: ApiError

  constructor(error: ApiError) {
    super(error.message)
    this.name = 'ApiRequestError'
    this.error = error
  }
}

export interface RequestOptions {
  /** Zone names in this language (`?lang=`); else the server uses `Accept-Language`. */
  lang?: string
  signal?: AbortSignal
  timeoutMs?: number
  /** Defaults to `API_BASE`. */
  base?: string
  /** Sent as `Authorization: Bearer <token>` (admin routes). */
  token?: string
}

export function apiUrl(path: string, lang?: string, base = API_BASE): string {
  const query = lang ? `${path.includes('?') ? '&' : '?'}lang=${encodeURIComponent(lang)}` : ''
  return `${base}${path}${query}`
}

function retryAfter(res: Response): number | undefined {
  const value = Number(res.headers.get('Retry-After'))
  return Number.isFinite(value) && value > 0 ? value : undefined
}

async function request<T>(
  path: string,
  isValid: (x: unknown) => x is T,
  { lang, signal, timeoutMs = TIMEOUT_MS, base = API_BASE, token }: RequestOptions = {},
  send?: { method: 'POST' | 'PATCH' | 'PUT' | 'DELETE'; body: unknown },
): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  const onAbort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    let res: Response
    let body: unknown
    try {
      res = await fetch(apiUrl(path, lang, base), {
        method: send?.method ?? 'GET',
        headers: {
          Accept: 'application/json',
          ...(send && { 'Content-Type': 'application/json' }),
          ...(token && { Authorization: `Bearer ${token}` }),
        },
        body: send ? JSON.stringify(send.body) : undefined,
        cache: 'no-store',
        signal: controller.signal,
      })
      body = await res.json().catch(() => undefined)
    } catch (err) {
      if (timedOut) {
        throw new ApiRequestError({
          code: 'timeout',
          message: `no answer in ${timeoutMs / 1000} s`,
        })
      }
      if (signal?.aborted) throw err
      throw new ApiRequestError({ code: 'network', message: String(err) })
    }
    if (!res.ok) {
      if (isApiErrorBody(body)) {
        throw new ApiRequestError({
          ...body.error,
          status: res.status,
          retryAfter: retryAfter(res),
        })
      }
      throw new ApiRequestError({
        code: 'bad_response',
        message: `HTTP ${res.status} without an error body`,
        status: res.status,
      })
    }
    if (!isValid(body)) {
      throw new ApiRequestError({
        code: 'bad_response',
        message: `unexpected response from ${path}`,
        status: res.status,
      })
    }
    return body
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

/** `GET /api/status`. Rejects with `unavailable` (503) until the server has data. */
export function getStatus(options?: RequestOptions): Promise<LotStatus> {
  if (IS_MOCK && !options?.base) return Promise.resolve(mockStatus())
  return request('/api/status', isLotStatus, options)
}

/** `GET /api/lot`: static lot info. */
export function getLot(options?: RequestOptions): Promise<LotInfo> {
  if (IS_MOCK && !options?.base) return Promise.resolve(mockLotInfo())
  return request('/api/lot', isLotInfo, options)
}

/** A JSON `POST`/`PATCH`/`DELETE` to the API (the push routes, api.md §2); never mocked here. */
export function sendJson<T>(
  method: 'POST' | 'PATCH' | 'DELETE',
  path: string,
  body: unknown,
  isValid: (x: unknown) => x is T,
  options?: RequestOptions,
): Promise<T> {
  return request(path, isValid, options, { method, body })
}

/** A `GET` of any other JSON route, checked with `isValid`; never mocked here. */
export function getRequest<T>(
  path: string,
  isValid: (x: unknown) => x is T,
  options?: RequestOptions,
): Promise<T> {
  return request(path, isValid, options)
}

// --- admin (api.md §4) ---

const ADMIN_TOKEN_KEY = 'parking.adminToken'
const tokenListeners = new Set<() => void>()

/** The admin session token, kept in sessionStorage (security-privacy.md §2: not localStorage). */
export function getAdminToken(): string | null {
  try {
    return sessionStorage.getItem(ADMIN_TOKEN_KEY)
  } catch {
    return null
  }
}

export function setAdminToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(ADMIN_TOKEN_KEY, token)
    else sessionStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch {
    // storage blocked: the admin just has to log in again
  }
  tokenListeners.forEach((listener) => listener())
}

/** For `useSyncExternalStore`: called whenever the token is set or forgotten. */
export function onAdminTokenChange(listener: () => void): () => void {
  tokenListeners.add(listener)
  return () => tokenListeners.delete(listener)
}

export interface AdminLogin {
  token: string
  expires_at: string
}

export interface AdminSession {
  actor: string
  expires_at: string | null
}

/** The mock build (no API) accepts this password, so the preview's admin screens can be tried. */
export const MOCK_ADMIN_PASSWORD = 'demo'
const MOCK_TOKEN = 'mock-admin-token'
const isObject = (x: unknown): x is Record<string, unknown> => typeof x === 'object' && x !== null
const isLogin = (x: unknown): x is AdminLogin =>
  isObject(x) && typeof x.token === 'string' && typeof x.expires_at === 'string'
const isSession = (x: unknown): x is AdminSession =>
  isObject(x) &&
  typeof x.actor === 'string' &&
  (x.expires_at === null || typeof x.expires_at === 'string')
const anyBody = (x: unknown): x is unknown => (void x, true) // logout's 204 has no body

function mockExpiry(): string {
  return new Date(Date.now() + 7 * 24 * 3600_000).toISOString()
}

/** `POST /api/admin/login`: stores and returns the 7-day session token. 401 wrong password,
 * 429 after 5 attempts in 15 min. */
export async function adminLogin(password: string, options?: RequestOptions): Promise<AdminLogin> {
  let login: AdminLogin
  if (IS_MOCK && !options?.base) {
    if (password !== MOCK_ADMIN_PASSWORD) {
      throw new ApiRequestError({ code: 'unauthorized', message: 'wrong password', status: 401 })
    }
    login = { token: MOCK_TOKEN, expires_at: mockExpiry() }
  } else {
    login = await request('/api/admin/login', isLogin, options, {
      method: 'POST',
      body: { password },
    })
  }
  setAdminToken(login.token)
  return login
}

/** An admin request with the stored token; a 401 forgets the token (→ login screen). */
export async function adminRequest<T>(
  path: string,
  isValid: (x: unknown) => x is T,
  options?: RequestOptions,
  send?: { method: 'POST' | 'PATCH' | 'PUT' | 'DELETE'; body?: unknown },
): Promise<T> {
  const token = getAdminToken()
  if (!token) {
    throw new ApiRequestError({ code: 'unauthorized', message: 'not logged in', status: 401 })
  }
  try {
    return await request(
      path,
      isValid,
      { ...options, token },
      send && { method: send.method, body: send.body ?? {} },
    )
  } catch (err) {
    if (err instanceof ApiRequestError && err.error.status === 401 && getAdminToken() === token) {
      setAdminToken(null)
    }
    throw err
  }
}

/** `GET /api/admin/session`: checks the stored token (401 → logged out). */
export function getAdminSession(options?: RequestOptions): Promise<AdminSession> {
  if (IS_MOCK && !options?.base) {
    if (getAdminToken() === MOCK_TOKEN) {
      return Promise.resolve({ actor: 'session:mock', expires_at: mockExpiry() })
    }
    setAdminToken(null) // like a 401 from the API
    return Promise.reject(
      new ApiRequestError({ code: 'unauthorized', message: 'not logged in', status: 401 }),
    )
  }
  return adminRequest('/api/admin/session', isSession, options)
}

/** `POST /api/admin/logout`: revokes the session; the token is forgotten even if that fails. */
export async function adminLogout(options?: RequestOptions): Promise<void> {
  try {
    if (!(IS_MOCK && !options?.base) && getAdminToken()) {
      await adminRequest('/api/admin/logout', anyBody, options, { method: 'POST' })
    }
  } finally {
    setAdminToken(null)
  }
}

// --- admin cameras (P7.2) ---

export type CameraState = 'ok' | 'degraded' | 'down' | 'unknown'

/** One row of `GET /api/admin/cameras` (the latest worker health message). */
export interface AdminCamera {
  id: string
  role: 'occupancy' | 'flow'
  zones: string[]
  state: CameraState
  issue: string | null
  fps: number | null
  last_frame_age_s: number | null
  inference_ms_avg: number | null
  unhealthy_ratio: number | null
  last_health_age_s: number | null
  /** The camera has a `control_url`, so the API can fetch a snapshot. */
  snapshot: boolean
}

const STATES: readonly string[] = ['ok', 'degraded', 'down', 'unknown']
const numOrNull = (x: unknown) => x === null || typeof x === 'number'
const isCamera = (x: unknown): x is AdminCamera =>
  isObject(x) &&
  typeof x.id === 'string' &&
  (x.role === 'occupancy' || x.role === 'flow') &&
  Array.isArray(x.zones) &&
  STATES.includes(x.state as string) &&
  (x.issue === null || typeof x.issue === 'string') &&
  numOrNull(x.fps) &&
  numOrNull(x.last_frame_age_s) &&
  numOrNull(x.inference_ms_avg) &&
  numOrNull(x.unhealthy_ratio) &&
  numOrNull(x.last_health_age_s) &&
  typeof x.snapshot === 'boolean'
const isCameraList = (x: unknown): x is AdminCamera[] => Array.isArray(x) && x.every(isCamera)

function mockCameras(): AdminCamera[] {
  return [
    {
      id: 'cam-ground',
      role: 'occupancy',
      zones: ['ground'],
      state: 'ok',
      issue: null,
      fps: 0.2,
      last_frame_age_s: 2.4,
      inference_ms_avg: 151,
      unhealthy_ratio: 0,
      last_health_age_s: 2.4,
      snapshot: true,
    },
    {
      id: 'cam-ramp',
      role: 'flow',
      zones: ['underground'],
      state: 'unknown',
      issue: null,
      fps: null,
      last_frame_age_s: null,
      inference_ms_avg: null,
      unhealthy_ratio: null,
      last_health_age_s: null,
      snapshot: false,
    },
  ]
}

function mockAdminOnly(): void {
  if (getAdminToken() !== MOCK_TOKEN) {
    setAdminToken(null)
    throw new ApiRequestError({ code: 'unauthorized', message: 'not logged in', status: 401 })
  }
}

/** `GET /api/admin/cameras`: every camera with its latest health. */
export async function getAdminCameras(options?: RequestOptions): Promise<AdminCamera[]> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    return mockCameras()
  }
  return adminRequest('/api/admin/cameras', isCameraList, options)
}

/** A placeholder picture for the mock build, which has no workers. */
function mockSnapshot(cameraId: string): Blob {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">' +
    '<rect width="100%" height="100%" fill="#334155"/>' +
    '<text x="50%" y="50%" fill="#e2e8f0" font-family="sans-serif" font-size="28" ' +
    `text-anchor="middle">${cameraId}: demo snapshot</text></svg>`
  return new Blob([svg], { type: 'image/svg+xml' })
}

/** `GET /api/admin/cameras/{id}/snapshot`: the worker's current frame as a JPEG blob (an
 * `<img src>` can't send the bearer header, so the caller shows it via a blob URL). The API
 * waits up to 5 s for the worker; `unavailable` (503) when it can't get one. */
export async function getCameraSnapshot(
  cameraId: string,
  { annotated = true, ...options }: RequestOptions & { annotated?: boolean } = {},
): Promise<Blob> {
  if (IS_MOCK && !options.base) {
    mockAdminOnly()
    if (cameraId !== 'cam-ground') {
      throw new ApiRequestError({ code: 'unavailable', message: 'no snapshot', status: 503 })
    }
    return mockSnapshot(cameraId)
  }
  const token = getAdminToken()
  if (!token) {
    throw new ApiRequestError({ code: 'unauthorized', message: 'not logged in', status: 401 })
  }
  const { signal, timeoutMs = TIMEOUT_MS, base = API_BASE } = options
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  const onAbort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', onAbort)
  try {
    let res: Response
    try {
      const path = `/api/admin/cameras/${encodeURIComponent(cameraId)}/snapshot`
      res = await fetch(`${base}${path}?annotated=${annotated}`, {
        headers: { Accept: 'image/jpeg', Authorization: `Bearer ${token}` },
        cache: 'no-store',
        signal: controller.signal,
      })
      if (res.ok) return await res.blob()
    } catch (err) {
      if (timedOut) {
        throw new ApiRequestError({
          code: 'timeout',
          message: `no answer in ${timeoutMs / 1000} s`,
        })
      }
      if (signal?.aborted) throw err
      throw new ApiRequestError({ code: 'network', message: String(err) })
    }
    const body: unknown = await res.json().catch(() => undefined)
    if (res.status === 401 && getAdminToken() === token) setAdminToken(null)
    throw new ApiRequestError(
      isApiErrorBody(body)
        ? { ...body.error, status: res.status, retryAfter: retryAfter(res) }
        : { code: 'bad_response', message: `HTTP ${res.status}`, status: res.status },
    )
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

// --- slot / line editor (P7.3) ---

export type CameraConfigKind = 'slots' | 'lines'

/** `PUT …/slots|lines`: saved (the old file kept as `.bak`) and whether the worker reloaded. */
export interface ConfigSaved {
  saved: string
  backup: boolean
  reloaded: boolean
  /** Why the worker didn't reload (the file is saved either way). */
  message: string | null
  slots?: number
}

/** `POST …/reference-frame`: where the worker saved its current frame. */
export interface ReferenceSaved {
  saved: string
  ts: string
}

const isConfigFile = (x: unknown): x is Record<string, unknown> =>
  isObject(x) && x.version === 1 && typeof x.camera_id === 'string'
const isConfigSaved = (x: unknown): x is ConfigSaved =>
  isObject(x) &&
  typeof x.saved === 'string' &&
  typeof x.backup === 'boolean' &&
  typeof x.reloaded === 'boolean' &&
  (x.message === null || typeof x.message === 'string')
const isReferenceSaved = (x: unknown): x is ReferenceSaved =>
  isObject(x) && typeof x.saved === 'string' && typeof x.ts === 'string'

// The mock build keeps edits in memory, on the 640×360 demo snapshot.
const mockConfigs = new Map<string, Record<string, unknown>>([
  [
    'cam-ground/slots',
    {
      version: 1,
      camera_id: 'cam-ground',
      image_size: [640, 360],
      slots: [
        {
          id: 'G01',
          zone: 'ground',
          polygon: [
            [60, 220],
            [160, 220],
            [160, 330],
            [60, 330],
          ],
          type: 'standard',
        },
        {
          id: 'G02',
          zone: 'ground',
          polygon: [
            [160, 220],
            [260, 220],
            [260, 330],
            [160, 330],
          ],
          type: 'standard',
        },
        {
          id: 'G03',
          zone: 'ground',
          polygon: [
            [260, 220],
            [360, 220],
            [360, 330],
            [260, 330],
          ],
          type: 'accessible',
        },
      ],
      count_zones: [],
    },
  ],
])

const configPath = (cameraId: string, kind: CameraConfigKind) =>
  `/api/admin/cameras/${encodeURIComponent(cameraId)}/${kind}`

/** `GET /api/admin/cameras/{id}/slots|lines`: the file as stored; 404 when there is none yet. */
export async function getCameraConfig(
  cameraId: string,
  kind: CameraConfigKind,
  options?: RequestOptions,
): Promise<Record<string, unknown>> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    const file = mockConfigs.get(`${cameraId}/${kind}`)
    if (!file) throw new ApiRequestError({ code: 'not_found', message: 'no file', status: 404 })
    return structuredClone(file)
  }
  return adminRequest(configPath(cameraId, kind), isConfigFile, options)
}

/** `PUT /api/admin/cameras/{id}/slots|lines`: validated by the API (422 with the reason), written
 * with a `.bak`, and the worker asked to reload it. */
export async function putCameraConfig(
  cameraId: string,
  kind: CameraConfigKind,
  file: Record<string, unknown>,
  options?: RequestOptions,
): Promise<ConfigSaved> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    const backup = mockConfigs.has(`${cameraId}/${kind}`)
    mockConfigs.set(`${cameraId}/${kind}`, structuredClone(file))
    const slots = Array.isArray(file.slots) ? file.slots.length : undefined
    return {
      saved: `config/${kind}/${cameraId}.json`,
      backup,
      reloaded: true,
      message: null,
      slots,
    }
  }
  return adminRequest(configPath(cameraId, kind), isConfigSaved, options, {
    method: 'PUT',
    body: file,
  })
}

/** `POST /api/admin/cameras/{id}/reference-frame`: the worker keeps its current frame as the
 * shift-detection reference (409 before its first frame, 503 unreachable). */
export async function saveReferenceFrame(
  cameraId: string,
  options?: RequestOptions,
): Promise<ReferenceSaved> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    return { saved: `data/reference/${cameraId}.jpg`, ts: new Date().toISOString() }
  }
  const path = `/api/admin/cameras/${encodeURIComponent(cameraId)}/reference-frame`
  return adminRequest(path, isReferenceSaved, options, { method: 'POST' })
}

// --- count corrections (P7.4) ---

/** One row of `GET /api/admin/corrections` (the audit log). */
export interface Correction {
  id: number
  ts: string
  zone_id: string
  /** In the requested language. */
  zone_name: string
  old_occupied: number
  new_occupied: number
  /** `admin-token`, `session:<id>` or `scheduled-reset`. */
  actor: string
  note: string
}

const isCorrection = (x: unknown): x is Correction =>
  isObject(x) &&
  typeof x.id === 'number' &&
  typeof x.ts === 'string' &&
  typeof x.zone_id === 'string' &&
  typeof x.zone_name === 'string' &&
  typeof x.old_occupied === 'number' &&
  typeof x.new_occupied === 'number' &&
  typeof x.actor === 'string' &&
  typeof x.note === 'string'
const isCorrectionList = (x: unknown): x is Correction[] =>
  Array.isArray(x) && x.every(isCorrection)
const isZoneStatus = (x: unknown): x is ZoneStatus =>
  isObject(x) &&
  typeof x.id === 'string' &&
  typeof x.occupied === 'number' &&
  typeof x.capacity === 'number'

// The mock build keeps its corrections in memory (newest first).
const mockLog: Correction[] = []

/** `POST /api/admin/zones/{id}/correct`: sets an entry/exit (`flow`) zone's count; the API
 * publishes the new status to every app. 409 for other zones, 422 above the capacity. */
export async function correctZone(
  zone: { id: string; name: string },
  occupied: number,
  note: string,
  options?: RequestOptions,
): Promise<ZoneStatus> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    const lot = mockLotInfo()
    const info = lot.zones.find((z) => z.id === zone.id)
    if (!info) throw new ApiRequestError({ code: 'not_found', message: 'no zone', status: 404 })
    if (info.method !== 'flow') {
      throw new ApiRequestError({ code: 'conflict', message: 'not a flow zone', status: 409 })
    }
    if (occupied > info.capacity) {
      throw new ApiRequestError({ code: 'bad_request', message: 'above capacity', status: 422 })
    }
    // the running mock feed's zone (its old count), else a one-off mock status
    const corrected = correctMockZone(zone.id, occupied)
    const old = corrected?.old ?? 0
    const after =
      corrected?.status.zones.find((z) => z.id === zone.id) ??
      mockStatus().zones.find((z) => z.id === zone.id)!
    mockLog.unshift({
      id: mockLog.length + 1,
      ts: new Date().toISOString(),
      zone_id: zone.id,
      zone_name: zone.name,
      old_occupied: old,
      new_occupied: occupied,
      actor: 'session:mock',
      note: note.trim(),
    })
    return { ...after, occupied, free: info.capacity - occupied }
  }
  const path = `/api/admin/zones/${encodeURIComponent(zone.id)}/correct`
  return adminRequest(path, isZoneStatus, options, {
    method: 'POST',
    body: { occupied, note: note.trim() },
  })
}

/** `GET /api/admin/corrections?limit=`: the newest corrections first. */
export async function getCorrections(limit = 50, options?: RequestOptions): Promise<Correction[]> {
  if (IS_MOCK && !options?.base) {
    mockAdminOnly()
    return mockLog.slice(0, limit).map((c) => ({ ...c }))
  }
  return adminRequest(`/api/admin/corrections?limit=${limit}`, isCorrectionList, options)
}
