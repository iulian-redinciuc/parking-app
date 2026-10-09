import { describe, expect, it } from 'vitest'
import type { HistoryPoint } from '../api/types'
import {
  dateKey,
  formatClock,
  hourlyMeans,
  hourOfDay,
  lotParts,
  quantile,
  startOfLotDay,
  todaySeries,
  typicalBand,
  weekdayName,
  weekHeatmap,
} from './stats'

const TZ = 'Europe/Bucharest'
const point = (t: string, free: number): HistoryPoint => ({
  t,
  free_avg: free,
  free_min: free,
  free_max: free,
  occupied_avg: 40 - free,
})

describe('lot-local time', () => {
  it('reads wall-clock parts and weekdays in the lot timezone', () => {
    // 2026-10-09 is a Friday; 21:30 UTC is 00:30 on Saturday in Bucharest (UTC+3)
    expect(lotParts(Date.parse('2026-10-09T21:30:00Z'), TZ)).toEqual({
      year: 2026,
      month: 10,
      day: 10,
      hour: 0,
      minute: 30,
      weekday: 6,
    })
    expect(dateKey(Date.parse('2026-10-09T20:59:00Z'), TZ)).toBe('2026-10-09')
    expect(hourOfDay(Date.parse('2026-10-09T10:45:00Z'), TZ)).toBe(13.75)
  })

  it('finds the local midnight, also on DST change days', () => {
    expect(new Date(startOfLotDay(Date.parse('2026-10-09T12:00:00Z'), TZ)).toISOString()).toBe(
      '2026-10-08T21:00:00.000Z',
    )
    // 2026-10-25: clocks go back at 04:00 local, midnight is still UTC+3
    expect(new Date(startOfLotDay(Date.parse('2026-10-25T20:00:00Z'), TZ)).toISOString()).toBe(
      '2026-10-24T21:00:00.000Z',
    )
    // the day after: UTC+2
    expect(new Date(startOfLotDay(Date.parse('2026-10-26T12:00:00Z'), TZ)).toISOString()).toBe(
      '2026-10-25T22:00:00.000Z',
    )
    expect(new Date(startOfLotDay(Date.parse('2026-10-09T12:00:00Z'), 'UTC')).toISOString()).toBe(
      '2026-10-09T00:00:00.000Z',
    )
  })

  it('formats clock times and weekday names in the UI language', () => {
    expect(formatClock(13.5, 'en-GB')).toBe('13:30')
    expect(formatClock(0, 'en-US')).toBe('12:00 AM')
    expect(weekdayName(1, 'en')).toBe('Mon')
    expect(weekdayName(7, 'en', true)).toBe('Sunday')
  })
})

describe('typical band and heatmap', () => {
  it('interpolates quantiles', () => {
    expect(quantile([1, 2, 3, 4], 0.5)).toBe(2.5)
    expect(quantile([1, 2, 3, 4, 5], 0.25)).toBe(2)
    expect(quantile([7], 0.75)).toBe(7)
  })

  it('takes the median and IQR per hour of the same weekday, skipping today and thin hours', () => {
    // Fridays 09:00 local (06:00 UTC) over 5 weeks; 10:00 only twice; one Thursday
    const points = [
      point('2026-09-04T06:00:00Z', 10),
      point('2026-09-11T06:00:00Z', 20),
      point('2026-09-18T06:00:00Z', 30),
      point('2026-09-25T06:00:00Z', 40),
      point('2026-10-09T06:00:00Z', 0), // today: left out
      point('2026-09-25T07:00:00Z', 5),
      point('2026-10-02T07:00:00Z', 6),
      point('2026-10-01T06:00:00Z', 99), // a Thursday
    ]
    const band = typicalBand(points, 5, TZ, '2026-10-09')
    expect(band).toEqual([{ hour: 9, median: 25, q1: 17.5, q3: 32.5, samples: 4 }])
  })

  it('averages free per weekday × hour', () => {
    const cells = weekHeatmap(
      [
        point('2026-10-05T06:00:00Z', 10), // Monday 09:00
        point('2026-09-28T06:00:00Z', 20), // Monday 09:00
        point('2026-10-11T20:00:00Z', 4), // Sunday 23:00
      ],
      TZ,
    )
    expect(cells).toHaveLength(7)
    expect(cells[0][9]).toBe(15)
    expect(cells[6][23]).toBe(4)
    expect(cells[1][9]).toBeNull()
  })
})

describe('today line', () => {
  it('keeps the lot-local day and averages 15-minute steps', () => {
    const points = [
      point('2026-10-08T20:59:00Z', 1), // 23:59 yesterday
      point('2026-10-08T21:00:00Z', 10),
      point('2026-10-08T21:14:00Z', 20),
      point('2026-10-08T21:15:00Z', 30),
      point('2026-10-09T10:30:00Z', 8),
    ]
    const series = todaySeries(points, TZ, '2026-10-09')
    expect(series).toEqual([
      { x: 0, free: 15 },
      { x: 0.25, free: 30 },
      { x: 13.5, free: 8 },
    ])
    expect(hourlyMeans(series)).toEqual(
      new Map([
        [0, 22.5],
        [13, 8],
      ]),
    )
  })
})
