import { type ComponentType, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiRequestError, getForecast, getHistory, getLot } from '../api/client'
import { IS_MOCK } from '../api/base'
import type { Forecast, History, LotInfo } from '../api/types'
import TodayLegend from '../components/charts/TodayLegend'
import WeekHeatmap from '../components/charts/WeekHeatmap'
import {
  dateKey,
  formatClock,
  HOUR_MS,
  hourlyMeans,
  lotParts,
  TYPICAL_WEEKS,
  todaySeries,
  typicalBand,
  weekdayName,
  weekHeatmap,
} from '../lib/stats'

// The Stats screen (frontend.md §2.3, P7.7), a lazy chunk with Recharts in a further lazy chunk:
// the "Usually ~N free at HH:MM" card (`/api/forecast` for now + 30 min), today's free spaces
// (`/api/history?bucket=minute`, the last 24 h cut to the lot-local day) over the typical band and
// the weekday × hour heatmap (both from one `bucket=hour` request over the 8 weeks before this
// hour). All requests go out together; days and hours are the lot's timezone.

// Fetched as soon as this chunk runs, in parallel with the data, and held in state rather than
// behind React.lazy + Suspense: a nested Suspense reveal waits up to 300 ms more (React 19's
// fallback throttle), which was a third of the load time on a phone.
type TodayChartType = typeof import('../components/charts/TodayChart').default
const chartModule = import('../components/charts/TodayChart')

function useTodayChart(): ComponentType<Parameters<TodayChartType>[0]> | null {
  const [chart, setChart] = useState<{ C: TodayChartType } | null>(null)
  useEffect(() => {
    let live = true
    void chartModule.then((m) => live && setChart({ C: m.default }))
    return () => {
      live = false
    }
  }, [])
  return chart?.C ?? null
}

const FORECAST_AHEAD_MS = 30 * 60_000
const MINUTE_MS = 60_000
const TOTAL = 'total'

interface Loaded {
  zone: string
  now: number
  today: History
  weeks: History
  forecast: Forecast | null
}

async function load(zone: string, signal: AbortSignal): Promise<Loaded> {
  const now = Math.floor(Date.now() / MINUTE_MS) * MINUTE_MS
  const hourStart = Math.floor(now / HOUR_MS) * HOUR_MS
  const iso = (ms: number) => new Date(ms).toISOString()
  const [today, weeks, forecast] = await Promise.all([
    getHistory({ zone, from: iso(now - 24 * HOUR_MS), to: iso(now), bucket: 'minute' }, { signal }),
    // whole hours, so everyone asking within the hour sends the same query (server cache)
    getHistory(
      { zone, from: iso(hourStart - (TYPICAL_WEEKS * 7 + 1) * 24 * HOUR_MS), to: iso(hourStart) },
      { signal },
    ),
    getForecast({ zone, at: iso(now + FORECAST_AHEAD_MS) }, { signal }).catch((err: unknown) => {
      if (err instanceof ApiRequestError && err.error.code === 'not_enough_data') return null
      throw err
    }),
  ])
  return { zone, now, today, weeks, forecast }
}

export default function StatsScreen() {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const [lot, setLot] = useState<LotInfo | null>(null)
  const [zone, setZone] = useState('total')
  const [data, setData] = useState<Loaded | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    getLot({ lang, signal: controller.signal }).then(setLot, () => {
      if (!controller.signal.aborted) setFailed(true)
    })
    return () => controller.abort()
  }, [lang, attempt])

  useEffect(() => {
    const controller = new AbortController()
    load(zone, controller.signal).then(
      (loaded) => {
        setData(loaded)
        setFailed(false)
      },
      () => {
        if (!controller.signal.aborted) setFailed(true)
      },
    )
    return () => controller.abort()
  }, [zone, attempt])

  const retry = () => {
    setFailed(false)
    setAttempt((n) => n + 1)
  }

  return (
    <section className="flex flex-col gap-4 py-4">
      <h1 className="text-2xl font-bold">{t('screen.stats')}</h1>
      {IS_MOCK && <p className="text-sm text-muted">{t('stats.mock_note')}</p>}
      {lot && lot.zones.length > 1 && (
        <div role="group" aria-label={t('stats.zone_picker')} className="flex flex-wrap gap-2">
          {[{ id: TOTAL, name: t('stats.total') }, ...lot.zones].map((z) => (
            <button
              key={z.id}
              type="button"
              aria-pressed={zone === z.id}
              onClick={() => setZone(z.id)}
              className="min-h-11 rounded-full border border-muted px-4 text-sm aria-pressed:border-accent aria-pressed:bg-accent aria-pressed:text-bg"
            >
              {z.name}
            </button>
          ))}
        </div>
      )}
      {failed ? (
        <div role="alert" className="flex flex-col items-start gap-3 rounded-xl bg-surface p-4">
          <p>{t('stats.error')}</p>
          <button
            type="button"
            onClick={retry}
            className="min-h-11 rounded-full bg-accent px-4 font-semibold text-bg"
          >
            {t('stats.retry')}
          </button>
        </div>
      ) : lot && data && data.zone === zone ? (
        <StatsBody lot={lot} data={data} />
      ) : (
        <p className="text-muted" role="status">
          {t('stats.loading')}
        </p>
      )}
    </section>
  )
}

function StatsBody({ lot, data }: { lot: LotInfo; data: Loaded }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const tz = lot.timezone
  const TodayChart = useTodayChart()
  const capacity =
    data.zone === 'total'
      ? lot.zones.reduce((sum, z) => sum + z.capacity, 0)
      : (lot.zones.find((z) => z.id === data.zone)?.capacity ?? 0)
  const weekday = lotParts(data.now, tz).weekday
  const weekdayLong = weekdayName(weekday, lang, true)

  // cheap (≤ 1 440 + 1 368 points) and only re-run when the data or the language changes
  const date = dateKey(data.now, tz)
  const today = todaySeries(data.today.points, tz, date)
  const band = typicalBand(data.weeks.points, weekday, tz, date)
  const cells = weekHeatmap(data.weeks.points, tz, date)

  const forecastTime = new Intl.DateTimeFormat(lang, {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: tz,
  }).format(data.forecast ? Date.parse(data.forecast.at) : data.now + FORECAST_AHEAD_MS)
  const hasHeatmap = cells.some((row) => row.some((v) => v !== null))

  return (
    <>
      <article className="rounded-xl bg-surface p-4" aria-labelledby="forecast-title">
        <h2 id="forecast-title" className="text-sm font-semibold text-muted">
          {t('stats.forecast_title')}
        </h2>
        {data.forecast ? (
          <>
            <p className="text-xl font-bold" data-testid="forecast">
              {t('stats.forecast', { count: data.forecast.free_expected, time: forecastTime })}
            </p>
            <p className="text-sm text-muted">
              {t('stats.forecast_basis', { count: data.forecast.samples })}
            </p>
          </>
        ) : (
          <p data-testid="forecast">{t('stats.forecast_none', { time: forecastTime })}</p>
        )}
      </article>

      <figure className="flex flex-col gap-2 rounded-xl bg-surface p-4">
        <h2 className="text-lg font-semibold">{t('stats.today_title')}</h2>
        <figcaption className="text-sm text-muted">
          {t('stats.today_caption', { weekday: weekdayLong })}
        </figcaption>
        {today.length === 0 && band.length === 0 ? (
          <p>{t('stats.today_none')}</p>
        ) : (
          <>
            {TodayChart ? (
              // eslint-disable-next-line react-hooks/static-components -- one module, set once
              <TodayChart today={today} band={band} capacity={capacity} />
            ) : (
              <div className="h-56" />
            )}
            <TodayLegend showBand={band.length > 0} />
            {today.length === 0 && <p className="text-sm">{t('stats.today_none')}</p>}
          </>
        )}
        {band.length === 0 && <p className="text-sm text-muted">{t('stats.no_typical')}</p>}
        <TodayTable today={hourlyMeans(today)} band={band} lang={lang} />
      </figure>

      <figure className="flex flex-col gap-2 rounded-xl bg-surface p-4">
        <h2 className="text-lg font-semibold">{t('stats.heatmap_title')}</h2>
        <figcaption className="text-sm text-muted">{t('stats.heatmap_caption')}</figcaption>
        {hasHeatmap ? (
          <>
            <WeekHeatmap cells={cells} />
            <HeatmapTable cells={cells} lang={lang} />
          </>
        ) : (
          <p>{t('stats.heatmap_none')}</p>
        )}
      </figure>
    </>
  )
}

const fmt = (value: number | null | undefined) =>
  value === null || value === undefined ? null : Math.round(value).toString()

/** The today chart as a table, for screen readers. */
function TodayTable({
  today,
  band,
  lang,
}: {
  today: Map<number, number>
  band: ReturnType<typeof typicalBand>
  lang: string
}) {
  const { t } = useTranslation()
  const byHour = new Map(band.map((b) => [b.hour, b]))
  const hours = Array.from({ length: 24 }, (_, h) => h).filter((h) => today.has(h) || byHour.has(h))
  const none = t('stats.no_value')
  return (
    // in an sr-only box: a table ignores the 1 px width of `sr-only` itself and widens the page
    <div className="sr-only">
      <table>
        <caption>{t('stats.today_title')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('stats.table_hour')}</th>
            <th scope="col">{t('stats.table_today')}</th>
            <th scope="col">{t('stats.table_typical')}</th>
            <th scope="col">{t('stats.table_range')}</th>
          </tr>
        </thead>
        <tbody>
          {hours.map((h) => {
            const b = byHour.get(h)
            return (
              <tr key={h}>
                <th scope="row">{formatClock(h, lang)}</th>
                <td>{fmt(today.get(h)) ?? none}</td>
                <td>{fmt(b?.median) ?? none}</td>
                <td>{b ? `${fmt(b.q1)}–${fmt(b.q3)}` : none}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** The heatmap as a table (weekday rows × hour columns), for screen readers. */
function HeatmapTable({ cells, lang }: { cells: (number | null)[][]; lang: string }) {
  const { t } = useTranslation()
  const none = t('stats.no_value')
  return (
    // in an sr-only box: a table ignores the 1 px width of `sr-only` itself and widens the page
    <div className="sr-only">
      <table>
        <caption>{t('stats.heatmap_caption')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('stats.table_weekday')}</th>
            {Array.from({ length: 24 }, (_, h) => (
              <th key={h} scope="col">
                {formatClock(h, lang)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cells.map((row, i) => (
            <tr key={i}>
              <th scope="row">{weekdayName(i + 1, lang, true)}</th>
              {row.map((v, h) => (
                <td key={h}>{fmt(v) ?? none}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
