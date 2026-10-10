import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'

// Under every screen, above the bottom nav (which is fixed: the padding keeps the link clear
// of it).
export default function Footer() {
  const { t } = useTranslation()
  return (
    <footer className="mx-auto flex w-full max-w-xl justify-center px-4 pb-[calc(4.5rem+env(safe-area-inset-bottom))]">
      <Link
        to="/privacy"
        className="flex min-h-11 items-center px-3 text-sm text-muted underline focus-visible:outline-2 focus-visible:outline-accent"
      >
        {t('screen.privacy')}
      </Link>
    </footer>
  )
}
