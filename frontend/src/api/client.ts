// REST client for the public API (docs/design/api.md §2). Every failure becomes an
// `ApiRequestError` carrying an `ApiError`; a caller's own abort is passed through unchanged.
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
  { lang, signal, timeoutMs = TIMEOUT_MS, base = API_BASE }: RequestOptions = {},
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
        headers: { Accept: 'application/json' },
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
