import { useTranslation } from 'react-i18next'
import { formatClock, weekdayName } from '../../lib/stats'

// "Busiest hours" (frontend.md §2.3): weekday × hour cells of the average free spaces, one hue
// light → dark as the lot gets fuller, scaled between the fullest and the emptiest cell (scaled to
// the capacity, a lot that never empties would be one dark block). Plain CSS grid (no chart library needed); each cell has a
// hover title, and screen readers get the table in StatsScreen instead (this figure is aria-hidden).

const HOUR_LABELS = [0, 6, 12, 18]

export default function WeekHeatmap({ cells }: { cells: (number | null)[][] }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const values = cells.flat().filter((v): v is number => v !== null)
  const range = { min: Math.min(...values), max: Math.max(...values) }
  return (
    <div aria-hidden="true" className="flex flex-col gap-2 text-xs" data-testid="heatmap">
      <div className="grid grid-cols-[2.5rem_repeat(24,minmax(0,1fr))] gap-0.5">
        <span />
        {Array.from({ length: 24 }, (_, hour) => (
          <span key={hour} className="whitespace-nowrap text-muted">
            {HOUR_LABELS.includes(hour) ? formatClock(hour, lang) : ''}
          </span>
        ))}
        {cells.map((row, i) => (
          <Row key={i} weekday={i + 1} row={row} range={range} lang={lang} t={t} />
        ))}
      </div>
      <div className="flex items-center gap-2 text-muted">
        <span>{t('stats.heatmap_more')}</span>
        <span className="h-3 w-24 rounded-sm bg-gradient-to-r from-accent/10 to-accent" />
        <span>{t('stats.heatmap_less')}</span>
      </div>
    </div>
  )
}

function Row({
  weekday,
  row,
  range,
  lang,
  t,
}: {
  weekday: number
  row: (number | null)[]
  range: { min: number; max: number }
  lang: string
  t: ReturnType<typeof useTranslation>['t']
}) {
  const day = weekdayName(weekday, lang)
  return (
    <>
      <span className="self-center text-muted">{day}</span>
      {row.map((free, hour) => {
        const time = formatClock(hour, lang)
        if (free === null) {
          return (
            <span
              key={hour}
              className="h-5 rounded-sm bg-surface"
              title={t('stats.heatmap_cell_none', { weekday: day, hour: time })}
            />
          )
        }
        // fuller = darker; never fully transparent, so an empty lot still shows a cell
        const spread = range.max - range.min
        const busy = spread > 0 ? (range.max - free) / spread : 0.5
        return (
          <span
            key={hour}
            className="h-5 rounded-sm bg-accent"
            style={{ opacity: 0.1 + 0.9 * busy }}
            data-free={Math.round(free)}
            title={t('stats.heatmap_cell', { weekday: day, hour: time, value: Math.round(free) })}
          />
        )
      })}
    </>
  )
}
