import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { isIosSafari, isStandalone } from '../lib/pwa'

const DISMISSED = 'parking.installHintDismissed'

function dismissedBefore() {
  try {
    return localStorage.getItem(DISMISSED) === '1'
  } catch {
    return false
  }
}

// iPhone/iPad Safari, not opened from the home screen (frontend.md §5): Safari has no install
// prompt, so say how. Installing is also what makes push work on iOS (notifications.md).
// `always` keeps it when dismissed before (the Alerts screen in Phase 6 needs it every time).
export default function InstallHint({ always = false }: { always?: boolean }) {
  const { t } = useTranslation()
  const [hidden, setHidden] = useState(
    () => !isIosSafari(navigator) || isStandalone() || (!always && dismissedBefore()),
  )
  if (hidden) return null
  const dismiss = () => {
    try {
      localStorage.setItem(DISMISSED, '1')
    } catch {
      // private mode: hide for this visit only
    }
    setHidden(true)
  }
  return (
    <aside
      data-testid="install-hint"
      aria-labelledby="install-hint-title"
      className="flex items-start gap-3 rounded-2xl border border-accent bg-surface p-4"
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        className="size-6 shrink-0 text-accent"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M12 15V3M8 7l4-4 4 4M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1" />
      </svg>
      <div className="flex-1">
        <h2 id="install-hint-title" className="font-semibold">
          {t('install.hint_title')}
        </h2>
        <p className="text-sm text-muted">{t('install.hint')}</p>
      </div>
      {!always && (
        <button
          type="button"
          onClick={dismiss}
          aria-label={t('install.dismiss')}
          className="-m-2 flex size-11 shrink-0 items-center justify-center rounded-full text-muted"
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            className="size-5"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
          >
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </button>
      )}
    </aside>
  )
}
