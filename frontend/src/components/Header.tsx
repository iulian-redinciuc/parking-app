import type { Connection } from '../api/types'

// The state is always shown as a word too, never only as a colour (frontend.md §4).
const CONNECTION: Record<Connection, { label: string; dot: string }> = {
  connecting: { label: 'Connecting', dot: 'bg-muted' },
  live: { label: 'Live', dot: 'bg-ok' },
  polling: { label: 'Updating', dot: 'bg-warn' },
  offline: { label: 'Offline', dot: 'bg-bad' },
  error: { label: 'No connection', dot: 'bg-bad' },
}

export default function Header({ connection }: { connection: Connection }) {
  const { label, dot } = CONNECTION[connection]
  return (
    <header className="sticky top-0 z-10 border-b border-surface bg-bg/95 pt-[env(safe-area-inset-top)] pr-[env(safe-area-inset-right)] pl-[env(safe-area-inset-left)] backdrop-blur">
      <div className="mx-auto flex h-14 max-w-xl items-center justify-between gap-3 px-4">
        <span className="truncate text-lg font-bold">Parking</span>
        <span className="flex shrink-0 items-center gap-2 text-sm text-muted" role="status">
          <span aria-hidden="true" className={`size-2.5 rounded-full ${dot}`} />
          {label}
        </span>
      </div>
    </header>
  )
}
