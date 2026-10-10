// @vitest-environment node
// P3.2 "Done when": the shared JSON examples match the TS types, at compile time (typed
// assignments below) and at run time (shape checks), and stay identical to the backend's copies.
/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import errorBadRequest from './__fixtures__/error-bad-request.json'
import errorNotFound from './__fixtures__/error-not-found.json'
import errorUnavailable from './__fixtures__/error-unavailable.json'
import lotInfo from './__fixtures__/lot-info.json'
import lotStatus from './__fixtures__/lot-status.json'
import lotStatusStale from './__fixtures__/lot-status-stale.json'
import type { ApiErrorBody, LotInfo, LotStatus } from './types'
import { isApiErrorBody, isLotInfo, isLotStatus } from './validate'

// JSON imports widen literals (`"plenty"` → string), so each is checked at run time first;
// `Widen<T>` then proves at compile time that the JSON has every field of T, with the right
// base types and nothing missing.
type Widen<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends null
        ? null
        : T extends (infer U)[]
          ? Widen<U>[]
          : T extends object
            ? { [K in keyof T]: Widen<T[K]> }
            : T

const statuses: Widen<LotStatus>[] = [lotStatus, lotStatusStale]
const infos: Widen<LotInfo>[] = [lotInfo]
const errors: Widen<ApiErrorBody>[] = [errorBadRequest, errorNotFound, errorUnavailable]

const BACKEND = new URL('../../../backend/tests/fixtures/api/', import.meta.url)
const FRONTEND = new URL('./__fixtures__/', import.meta.url)
const jsonFiles = (dir: URL) =>
  readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .sort()

describe('shared API fixtures', () => {
  it.each(statuses.map((s) => [s.zones[0].name, s]))('LotStatus (%s)', (_, data) => {
    expect(isLotStatus(data)).toBe(true)
    const status = data as LotStatus
    expect(status.total.free).toBe(status.zones.reduce((n, z) => n + z.free, 0))
    expect(status.total.stale).toBe(status.zones.some((z) => z.stale))
  })

  it('LotInfo', () => {
    expect(infos.every(isLotInfo)).toBe(true)
  })

  it('error bodies', () => {
    expect(errors.every(isApiErrorBody)).toBe(true)
  })

  it('the checks reject wrong shapes', () => {
    const status = structuredClone(lotStatus)
    status.zones[0].level = 'roomy'
    expect(isLotStatus(status)).toBe(false)
    expect(isLotStatus({ ...lotStatus, v: 2 })).toBe(false)
    expect(isLotStatus({ ...lotStatus, zones: [{ ...lotStatus.zones[1], slots: [] }] })).toBe(false)
    // by_type (P9.2): absent from an older server, never a malformed count
    const older: Record<string, unknown> = { ...lotStatus.zones[0] }
    delete older.by_type
    expect(isLotStatus({ ...lotStatus, zones: [older] })).toBe(true)
    for (const by_type of [
      { ev: 1 },
      { ev: { capacity: 1 } },
      { ev: { capacity: 1, free: -1 } },
      [],
    ])
      expect(isLotStatus({ ...lotStatus, zones: [{ ...lotStatus.zones[0], by_type }] })).toBe(false)
    expect(isLotStatus({ ...lotStatus, total: { ...lotStatus.total, confidence: 1.2 } })).toBe(
      false,
    )
    expect(isLotInfo({ ...lotInfo, location: null })).toBe(false)
    expect(isApiErrorBody({ error: { code: 'oops', message: 'x' } })).toBe(false)
    expect(isApiErrorBody({ detail: 'Not Found' })).toBe(false)
  })

  it('are the same files as the backend copies', () => {
    const names = jsonFiles(BACKEND)
    expect(names.length).toBeGreaterThan(0)
    expect(jsonFiles(FRONTEND)).toEqual(names)
    for (const name of names) {
      const copy = readFileSync(new URL(name, FRONTEND), 'utf8')
      expect(copy, `${name}: copy backend/tests/fixtures/api/ again`).toBe(
        readFileSync(new URL(name, BACKEND), 'utf8'),
      )
    }
  })
})
