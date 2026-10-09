// Runtime shape checks for API responses (the server is another program: trust, but verify).
import type {
  ApiErrorBody,
  Forecast,
  History,
  LotInfo,
  LotStatus,
  Totals,
  ZoneStatus,
} from './types'

type Obj = Record<string, unknown>

const LEVELS = ['plenty', 'filling', 'almost_full', 'full']
const TRENDS = ['filling', 'emptying', 'steady']
const METHODS = ['slots', 'count', 'flow']
const ERROR_CODES = [
  'bad_request',
  'unauthorized',
  'forbidden',
  'not_found',
  'not_enough_data',
  'method_not_allowed',
  'conflict',
  'rate_limited',
  'unavailable',
  'internal',
]

const isObj = (x: unknown): x is Obj => typeof x === 'object' && x !== null && !Array.isArray(x)
const isStr = (x: unknown): x is string => typeof x === 'string'
const isCount = (x: unknown): x is number => Number.isInteger(x) && (x as number) >= 0
const isRatio = (x: unknown): x is number => typeof x === 'number' && x >= 0 && x <= 1
const isTs = (x: unknown): x is string => isStr(x) && !Number.isNaN(Date.parse(x))
const oneOf = (values: string[]) => (x: unknown) => isStr(x) && values.includes(x)

function isCounts(x: Obj): boolean {
  return (
    isCount(x.capacity) &&
    isCount(x.occupied) &&
    isCount(x.free) &&
    oneOf(LEVELS)(x.level) &&
    isRatio(x.confidence) &&
    typeof x.stale === 'boolean'
  )
}

export function isTotals(x: unknown): x is Totals {
  return isObj(x) && isCounts(x)
}

export function isZoneStatus(x: unknown): x is ZoneStatus {
  return (
    isObj(x) &&
    isStr(x.id) &&
    isStr(x.name) &&
    oneOf(METHODS)(x.method) &&
    isCounts(x) &&
    oneOf(TRENDS)(x.trend) &&
    (x.updated_at === null || isTs(x.updated_at)) &&
    (x.slots === null ||
      (isObj(x.slots) && Object.values(x.slots).every((v) => typeof v === 'boolean')))
  )
}

export function isLotStatus(x: unknown): x is LotStatus {
  return (
    isObj(x) &&
    x.v === 1 &&
    isStr(x.lot) &&
    isTs(x.updated_at) &&
    isTotals(x.total) &&
    Array.isArray(x.zones) &&
    x.zones.every(isZoneStatus)
  )
}

export function isLotInfo(x: unknown): x is LotInfo {
  return (
    isObj(x) &&
    x.v === 1 &&
    isStr(x.id) &&
    isStr(x.name) &&
    isObj(x.location) &&
    typeof x.location.lat === 'number' &&
    typeof x.location.lon === 'number' &&
    isCount(x.notify_radius_m) &&
    isStr(x.timezone) &&
    Array.isArray(x.zones) &&
    x.zones.every(
      (z) =>
        isObj(z) && isStr(z.id) && isStr(z.name) && oneOf(METHODS)(z.method) && isCount(z.capacity),
    ) &&
    isObj(x.levels) &&
    isRatio(x.levels.plenty) &&
    isRatio(x.levels.filling)
  )
}

export function isApiErrorBody(x: unknown): x is ApiErrorBody {
  if (!isObj(x) || !isObj(x.error)) return false
  const { code, message, details } = x.error
  return (
    oneOf(ERROR_CODES)(code) &&
    isStr(message) &&
    (details === undefined ||
      (Array.isArray(details) &&
        details.every((d) => isObj(d) && isStr(d.type) && Array.isArray(d.loc) && isStr(d.msg))))
  )
}

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)

export function isHistory(x: unknown): x is History {
  return (
    isObj(x) &&
    isStr(x.zone) &&
    oneOf(['minute', 'hour', 'day'])(x.bucket) &&
    isTs(x.from) &&
    isTs(x.to) &&
    Array.isArray(x.points) &&
    x.points.every(
      (p) =>
        isObj(p) &&
        isTs(p.t) &&
        isNum(p.free_avg) &&
        isNum(p.free_min) &&
        isNum(p.free_max) &&
        isNum(p.occupied_avg),
    )
  )
}

export function isForecast(x: unknown): x is Forecast {
  return (
    isObj(x) &&
    isStr(x.zone) &&
    isTs(x.at) &&
    isCount(x.free_expected) &&
    isStr(x.basis) &&
    isCount(x.samples)
  )
}
