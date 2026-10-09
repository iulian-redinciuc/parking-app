import { useTranslation } from 'react-i18next'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatClock, type HourBand, type TodayPoint } from '../../lib/stats'

// "Today" (frontend.md §2.3): today's free spaces as a line over the typical band for this weekday
// (median dashed, the middle half shaded). Drawn in the theme tokens, so it reads in light and dark;
// screen readers get the table in StatsScreen instead (this figure is aria-hidden).

interface Row {
  x: number
  free?: number
  median?: number
  range?: [number, number]
  /** The hour's band, for the tooltip on rows between band points. */
  band?: HourBand
}

const TICKS = [0, 6, 12, 18, 24]
const fmt = (value: number) => Math.round(value).toString()

function rows(today: TodayPoint[], band: HourBand[]): Row[] {
  const byHour = new Map(band.map((b) => [b.hour, b]))
  const byX = new Map<number, Row>()
  for (const b of band) {
    byX.set(b.hour + 0.5, { x: b.hour + 0.5, median: b.median, range: [b.q1, b.q3], band: b })
  }
  for (const p of today) {
    const row = byX.get(p.x) ?? { x: p.x, band: byHour.get(Math.floor(p.x)) }
    byX.set(p.x, { ...row, free: p.free })
  }
  return [...byX.values()].sort((a, b) => a.x - b.x)
}

export default function TodayChart({
  today,
  band,
  capacity,
}: {
  today: TodayPoint[]
  band: HourBand[]
  capacity: number
}) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const data = rows(today, band)
  return (
    <div aria-hidden="true" className="h-56 w-full text-xs" data-testid="today-chart">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 16, left: 0 }}>
          <CartesianGrid stroke="var(--muted)" strokeOpacity={0.25} vertical={false} />
          <XAxis
            dataKey="x"
            type="number"
            domain={[0, 24]}
            ticks={TICKS}
            tickFormatter={(x: number) => formatClock(x % 24, lang)}
            tick={{ fill: 'var(--muted)' }}
            stroke="var(--muted)"
            label={{
              value: t('stats.axis_time'),
              position: 'insideBottom',
              offset: -12,
              fill: 'var(--muted)',
            }}
          />
          <YAxis
            domain={[0, capacity]}
            allowDecimals={false}
            width={44}
            tick={{ fill: 'var(--muted)' }}
            stroke="var(--muted)"
            label={{
              value: t('stats.axis_free'),
              angle: -90,
              position: 'insideLeft',
              offset: 12,
              fill: 'var(--muted)',
              style: { textAnchor: 'middle' },
            }}
          />
          <Area
            dataKey="range"
            stroke="none"
            fill="var(--muted)"
            fillOpacity={0.25}
            connectNulls
            isAnimationActive={false}
            activeDot={false}
          />
          <Line
            dataKey="median"
            stroke="var(--muted)"
            strokeWidth={2}
            strokeDasharray="4 4"
            dot={false}
            connectNulls
            isAnimationActive={false}
            activeDot={false}
          />
          <Line
            dataKey="free"
            stroke="var(--accent)"
            strokeWidth={2}
            dot={false}
            connectNulls
            isAnimationActive={false}
            activeDot={{ r: 4, stroke: 'var(--bg)', strokeWidth: 2, fill: 'var(--accent)' }}
          />
          <Tooltip
            cursor={{ stroke: 'var(--muted)', strokeWidth: 1 }}
            content={({ active, payload }) => {
              const row = payload?.[0]?.payload as Row | undefined
              if (!active || !row) return null
              return (
                <div className="rounded-lg border border-muted/40 bg-bg px-3 py-2 text-sm text-text shadow">
                  <p className="font-semibold">{formatClock(row.x, lang)}</p>
                  {row.free !== undefined && (
                    <p>{t('stats.tooltip_today', { value: fmt(row.free) })}</p>
                  )}
                  {row.band && (
                    <>
                      <p className="text-muted">
                        {t('stats.tooltip_typical', { value: fmt(row.band.median) })}
                      </p>
                      <p className="text-muted">
                        {t('stats.tooltip_range', {
                          low: fmt(row.band.q1),
                          high: fmt(row.band.q3),
                        })}
                      </p>
                    </>
                  )}
                </div>
              )
            }}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
