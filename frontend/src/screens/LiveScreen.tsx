import { useState } from 'react'
import BigCount from '../components/BigCount'
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
  const live = useLiveStatus()
  const now = useNow()
  const [since] = useState(Date.now)
  const { banner, loading, status, dimAll } = liveView(live, now, since)
  return (
    <div className="flex flex-col gap-3 pb-4">
      <h1 className="sr-only">Live</h1>
      {banner && <StatusBanner banner={banner} />}
      {loading && <LiveSkeleton />}
      {status && (
        <>
          <BigCount total={status.total} dimmed={dimAll} />
          <ul className="flex flex-col gap-3" aria-label="Zones">
            {status.zones.map((zone) => (
              <ZoneCard key={zone.id} zone={zone} dimmed={dimAll || zone.stale} />
            ))}
          </ul>
          <UpdatedAgo at={live.lastMessageAt} />
        </>
      )}
    </div>
  )
}
