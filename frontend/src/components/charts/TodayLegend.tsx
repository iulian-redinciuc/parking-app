import { useTranslation } from 'react-i18next'

// Kept apart from TodayChart so the Stats chunk doesn't pull in Recharts before the chart.

/** The legend under the chart: the marks drawn the same way, with words (never colour alone). */
export default function TodayLegend({ showBand }: { showBand: boolean }) {
  const { t } = useTranslation()
  return (
    <ul aria-hidden="true" className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted">
      <li className="flex items-center gap-2">
        <span className="h-0.5 w-5 rounded bg-accent" />
        {t('stats.legend_today')}
      </li>
      {showBand && (
        <>
          <li className="flex items-center gap-2">
            <span className="w-5 border-t-2 border-dashed border-muted" />
            {t('stats.legend_median')}
          </li>
          <li className="flex items-center gap-2">
            <span className="h-3 w-5 rounded-sm bg-muted/25" />
            {t('stats.legend_band')}
          </li>
        </>
      )}
    </ul>
  )
}
