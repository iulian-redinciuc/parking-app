// REST client for the API (docs/design/api.md §2, §4). Every failure becomes an
// `ApiRequestError` carrying an `ApiError`; a caller's own abort is passed through unchanged.
// Admin calls add `Authorization: Bearer <token>` from sessionStorage; a 401 forgets the token,
// which sends the admin screens back to the login.
import { API_BASE, IS_MOCK } from './base'
import { mockLotInfo, mockStatus } from './mock'
import type { ApiError, LotInfo, LotStatus } from './types'
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
  const query = lang ? `?lang=${encodeURIComponent(lang)}` : ''
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
