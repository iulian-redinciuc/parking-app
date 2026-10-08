import { useTranslation } from 'react-i18next'
import { useCanInstall } from '../hooks/useInstall'
import { promptInstall } from '../lib/install'

// Header "Install app" button (Android/desktop), only while the browser offers it. Icon-only on
// narrow screens so the header fits at 320 px; the name is always "Install app".
export default function InstallButton() {
  const { t } = useTranslation()
  if (!useCanInstall()) return null
  return (
    <button
      type="button"
      onClick={() => void promptInstall()}
      className="flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 rounded-full border border-surface px-2 text-sm font-semibold text-accent"
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        className="size-5"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M12 3v12M7 10l5 5 5-5M5 21h14" />
      </svg>
      <span className="sr-only sm:not-sr-only">{t('install.button')}</span>
    </button>
  )
}
