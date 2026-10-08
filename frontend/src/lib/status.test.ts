import { describe, expect, it } from 'vitest'
import { LEVELS, TRENDS, formatFree, isEstimated, levelInfo, occupiedShare } from './status'

describe('status helpers', () => {
  it.each([
    ['plenty', 'level.plenty', 'Plenty of space', 'ok'],
    ['filling', 'level.filling', 'Filling up', 'warn'],
    ['almost_full', 'level.almost_full', 'Almost full', 'bad'],
    ['full', 'level.full', 'Full', 'bad'],
  ] as const)('maps level %s to a key, word and colour', (level, key, label, tone) => {
    expect(levelInfo(level)).toEqual({ key, label, tone })
  })

  it('has an i18n key for every level and trend', () => {
    expect(Object.keys(LEVELS)).toHaveLength(4)
    expect(Object.values(TRENDS).map((t) => t.key)).toEqual([
      'trend.filling',
      'trend.emptying',
      'trend.steady',
    ])
  })

  it('adds ≈ below 0.8 confidence only', () => {
    expect(formatFree({ free: 12, confidence: 1 })).toBe('12')
    expect(formatFree({ free: 12, confidence: 0.8 })).toBe('12')
    expect(formatFree({ free: 11, confidence: 0.79 })).toBe('≈ 11')
    expect(formatFree({ free: 0, confidence: 0 })).toBe('≈ 0')
    expect(isEstimated(0.86)).toBe(false)
  })

  it('clamps the taken share', () => {
    expect(occupiedShare({ capacity: 40, occupied: 28 })).toBe(0.7)
    expect(occupiedShare({ capacity: 0, occupied: 3 })).toBe(0)
    expect(occupiedShare({ capacity: 10, occupied: 12 })).toBe(1)
  })
})
