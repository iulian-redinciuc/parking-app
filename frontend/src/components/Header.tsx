import { useTranslation } from 'react-i18next'
import type { Connection } from '../api/types'
import InstallButton from './InstallButton'

// The state is always shown as a word too, never only as a colour (frontend.md §4).
// The word is `connection.<state>` in the locale files.
const DOT: Record<Connection, string> = {
  connecting: 'bg-muted',
  live: 'bg-ok',
  polling: 'bg-warn',
  offline: 'bg-bad',
  error: 'bg-bad',
}

export default function Header({ connection }: { connection: Connection }) {
  const { t } = useTranslation()
  const dot = DOT[connection]
  return (
    <header className="sticky top-0 z-10 border-b border-surface bg-bg/95 pt-[env(safe-area-inset-top)] pr-[env(safe-area-inset-right)] pl-[env(safe-area-inset-left)] backdrop-blur">
      <div className="mx-auto flex h-14 max-w-xl items-center justify-between gap-3 px-4">
        <span className="flex-1 truncate text-lg font-bold">{t('app.name')}</span>
        <InstallButton />
        <span className="flex shrink-0 items-center gap-2 text-sm text-muted" role="status">
          <span aria-hidden="true" className={`size-2.5 rounded-full ${dot}`} />
          {t(`connection.${connection}`)}
        </span>
      </div>
    </header>
  )
}
