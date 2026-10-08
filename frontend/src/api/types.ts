// Contract types, copied from docs/design/api.md §1–§2 (backend: parking/messages.py).
// Timestamps are UTC ISO 8601 strings with `Z`.

export type Level = 'plenty' | 'filling' | 'almost_full' | 'full'
export type Trend = 'filling' | 'emptying' | 'steady'
export type ZoneMethod = 'slots' | 'count' | 'flow'

export interface Totals {
  capacity: number
  occupied: number
  free: number
  level: Level
  /** The lowest zone confidence (0–1). */
  confidence: number
  /** `true` if any zone is stale. */
  stale: boolean
}

export interface ZoneStatus {
  id: string
  /** Already in the requested language. */
  name: string
  method: ZoneMethod
  capacity: number
  occupied: number
  free: number
  level: Level
  confidence: number
  stale: boolean
  trend: Trend
  /** `null` until the zone has received data. */
  updated_at: string | null
  /** Slot id → taken, only for `slots` zones. */
  slots: Record<string, boolean> | null
}

/** `GET /api/status` and every SSE `status` event. */
export interface LotStatus {
  v: 1
  lot: string
  updated_at: string
  total: Totals
  zones: ZoneStatus[]
}

export interface LotZoneInfo {
  id: string
  name: string
  method: ZoneMethod
  capacity: number
}

/** `GET /api/lot`. */
export interface LotInfo {
  v: 1
  id: string
  name: string
  location: { lat: number; lon: number }
  notify_radius_m: number
  timezone: string
  zones: LotZoneInfo[]
  levels: { plenty: number; filling: number }
}

/** Codes the server sends (api.md §1 error format). */
export type ServerErrorCode =
  | 'bad_request'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'method_not_allowed'
  | 'conflict'
  | 'rate_limited'
  | 'unavailable'
  | 'internal'

/** Added by the client: no answer (`network`), no answer in 10 s (`timeout`), not our JSON (`bad_response`). */
export type ClientErrorCode = 'network' | 'timeout' | 'bad_response'

export interface ApiErrorDetail {
  type: string
  loc: (string | number)[]
  msg: string
}

export interface ApiError {
  code: ServerErrorCode | ClientErrorCode
  message: string
  details?: ApiErrorDetail[]
  /** HTTP status; absent for client-side errors. */
  status?: number
  /** Seconds, from `Retry-After` (429). */
  retryAfter?: number
}

/** The body of every error response. */
export interface ApiErrorBody {
  error: { code: ServerErrorCode; message: string; details?: ApiErrorDetail[] }
}

// Live data (docs/design/frontend.md §3): shared by the live manager (live.ts) and the mock.

export type Connection = 'connecting' | 'live' | 'polling' | 'offline' | 'error'

export interface LiveState {
  status: LotStatus | null
  connection: Connection
  /** `Date.now()` of the last event or poll. */
  lastMessageAt: number | null
  error: ApiError | null
}

/** A store for `useSyncExternalStore`, plus start/stop of the connection. */
export interface LiveFeed {
  getSnapshot(): LiveState
  subscribe(listener: () => void): () => void
  start(): void
  stop(): void
}
