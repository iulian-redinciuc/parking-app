import { describe, expect, it } from 'vitest'
import { lotParts } from '../lib/stats'
import { ApiRequestError, getForecast, getHistory } from './client'
import { mockForecast, mockHistory } from './mockHistory'
import { isForecast, isHistory } from './validate'

const NOW = Date.parse('2026-10-09T10:07:00Z')

describe('mock history and forecast', () => {
  it('answers 8 weeks of hours and a day of minutes, valid against the contract', () => {
    const weeks = mockHistory(
      { from: '2026-08-13T10:00:00Z', to: '2026-10-09T10:00:00Z', bucket: 'hour' },
      NOW,
    )
    expect(isHistory(weeks)).toBe(true)
    expect(weeks.points).toHaveLength(57 * 24)
    const minutes = mockHistory(
      { zone: 'ground', from: '2026-10-08T10:07:00Z', bucket: 'minute' },
      NOW,
    )
    expect(minutes.points).toHaveLength(1440)
    expect(minutes.points.every((p) => p.free_avg >= 0 && p.free_avg <= 40)).toBe(true)
  })

  it('is busier on workday middays than at night', () => {
    const at = (iso: string) => mockForecast({ at: iso }, NOW).free_expected
    expect(lotParts(Date.parse('2026-10-08T09:30:00Z'), 'Europe/Bucharest').weekday).toBe(4)
    expect(at('2026-10-08T09:30:00Z')).toBeLessThan(at('2026-10-08T00:30:00Z'))
    expect(isForecast(mockForecast({}, NOW))).toBe(true)
  })

  it('rejects unknown zones and too many buckets like the API', () => {
    expect(() => mockHistory({ zone: 'roof' }, NOW)).toThrow(ApiRequestError)
    expect(() =>
      mockHistory(
        { from: '2026-01-01T00:00:00Z', to: '2026-10-01T00:00:00Z', bucket: 'hour' },
        NOW,
      ),
    ).toThrow(ApiRequestError)
  })

  it('is what the client returns in mock mode', async () => {
    expect((await getHistory({ zone: 'underground' })).zone).toBe('underground')
    expect((await getForecast({ zone: 'ground' })).samples).toBe(8)
  })
})
