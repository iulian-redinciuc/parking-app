import BigCount from '../components/BigCount'
import UpdatedAgo from '../components/UpdatedAgo'
import ZoneCard from '../components/ZoneCard'
import { useLiveStatus } from '../hooks/useLiveStatus'

// The Live screen (frontend.md §2.1). Banners, the loading skeleton and greyed-out stale numbers
// come with StatusBanner (P3.5).
export default function LiveScreen() {
  const { status, lastMessageAt } = useLiveStatus()
  return (
    <div className="flex flex-col gap-3 pb-4">
      <h1 className="sr-only">Live</h1>
      {status === null ? (
        <p className="py-8 text-center text-muted">Waiting for data…</p>
      ) : (
        <>
          <BigCount total={status.total} />
          <ul className="flex flex-col gap-3" aria-label="Zones">
            {status.zones.map((zone) => (
              <ZoneCard key={zone.id} zone={zone} />
            ))}
          </ul>
          <UpdatedAgo at={lastMessageAt} />
        </>
      )}
    </div>
  )
}
