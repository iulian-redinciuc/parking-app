import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import BigCount from '../components/BigCount'
import InstallHint from '../components/InstallHint'
import StatusBanner, { LiveSkeleton } from '../components/StatusBanner'
import UpdatedAgo from '../components/UpdatedAgo'
import ZoneCard from '../components/ZoneCard'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useNow } from '../hooks/useNow'
import { liveView } from '../lib/banner'

// The Live screen (frontend.md §2.1): one StatusBanner for the highest-priority edge state, a
// skeleton until the first answer, and old numbers greyed (not hidden) when offline, unreachable
// or stale.
export default function LiveScreen() {
  const { t } = useTranslation()
  const live = useLiveStatus()
  const now = useNow()
  const [since] = useState(Date.now)
  const { banner, loading, status, dimAll } = liveView(live, now, since)
  return (
    <div className="flex flex-col gap-3 pb-4">
      <h1 className="sr-only">{t('screen.live')}</h1>
      {banner && <StatusBanner banner={banner} />}
      {loading && <LiveSkeleton />}
      {status && (
        <>
          <BigCount total={status.total} dimmed={dimAll} />
          <ul className="flex flex-col gap-3" aria-label={t('zone.list')}>
            {status.zones.map((zone) => (
              <ZoneCard key={zone.id} zone={zone} dimmed={dimAll || zone.stale} />
            ))}
          </ul>
          <UpdatedAgo at={live.lastMessageAt} />
        </>
      )}
      <InstallHint />
    </div>
  )
}
