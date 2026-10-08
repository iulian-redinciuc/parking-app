import { useNow } from '../hooks/useNow'
import { formatUpdatedAgo } from '../lib/time'

// When the app last heard from the server (an event, a ping or a poll), re-rendered every second.
export default function UpdatedAgo({ at }: { at: number | null }) {
  const now = useNow()
  if (at === null) return null
  return (
    <p className="text-sm text-muted">
      <time dateTime={new Date(at).toISOString()}>{formatUpdatedAgo(at, now)}</time>
    </p>
  )
}
