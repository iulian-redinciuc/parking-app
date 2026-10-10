import { useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { getLots } from '../api/client'
import { selectLot, selectedLot } from '../api/lots'
import type { LotList } from '../api/types'
import { followSelectedLot } from '../lib/push'

interface Props {
  /** Shown instead while the server has a single lot (or hasn't answered). */
  fallback: ReactNode
  /** Starts the app again on the new lot; replaced in tests. */
  reload?: () => void
}

// The lot picker (docs/design/multi-lot.md §6), in the header in place of the app's name when
// the server counts more than one lot. Picking a lot stores it on this device, moves the push
// subscription and reloads: the live connection, the screens and their caches all belong to one
// lot, so the app simply starts again on the other one.
export default function LotPicker({ fallback, reload = () => window.location.reload() }: Props) {
  const { t } = useTranslation()
  const [list, setList] = useState<LotList | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    getLots({ signal: controller.signal }).then(
      (lots) => {
        const picked = selectedLot()
        if (picked !== null && !lots.lots.some((lot) => lot.id === picked)) {
          // the picked lot is gone from the server: back to its default lot
          selectLot(null)
          void followSelectedLot().finally(reload)
          return
        }
        setList(lots)
      },
      () => undefined, // an older server, or offline: no picker
    )
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per app start
  }, [])

  if (!list || list.lots.length < 2) return fallback

  const change = (id: string) => {
    selectLot(id)
    void followSelectedLot().finally(reload)
  }

  return (
    <select
      aria-label={t('lots.choose')}
      value={selectedLot() ?? list.default}
      onChange={(e) => change(e.target.value)}
      className="min-h-11 min-w-0 flex-1 truncate rounded-lg border border-surface bg-bg px-2 text-lg font-bold"
    >
      {list.lots.map((lot) => (
        <option key={lot.id} value={lot.id}>
          {lot.name}
        </option>
      ))}
    </select>
  )
}
