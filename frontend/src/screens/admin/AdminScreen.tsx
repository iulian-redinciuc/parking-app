import { useEffect, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { Route, Routes } from 'react-router'
import {
  adminLogout,
  ApiRequestError,
  getAdminSession,
  getAdminToken,
  onAdminTokenChange,
  type AdminSession,
} from '../../api/client'
import CameraDetail from './CameraDetail'
import Cameras from './Cameras'
import Login from './Login'

// `#/admin` (frontend.md §2.4), a lazy chunk: the login without a token, else the admin home
// (session + camera list) and `#/admin/cameras/<id>` (camera detail with a snapshot).
// A 401 from any admin call forgets the token (client.ts), which brings the login back.

function useAdminToken(): string | null {
  return useSyncExternalStore(onAdminTokenChange, getAdminToken, () => null)
}

function AdminHome({ token }: { token: string }) {
  const { t, i18n } = useTranslation()
  const [session, setSession] = useState<AdminSession | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let active = true
    getAdminSession().then(
      (s) => active && setSession(s),
      (err: unknown) => {
        // a 401 already forgot the token; anything else (offline) keeps it for a retry
        if (active && !(err instanceof ApiRequestError && err.error.status === 401)) setFailed(true)
      },
    )
    return () => {
      active = false
    }
  }, [token])

  const until = session?.expires_at
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(
        new Date(session.expires_at),
      )
    : null

  return (
    <section className="flex flex-col gap-4 py-8">
      <h1 className="text-2xl font-bold">{t('admin.title')}</h1>
      <div role="status" className="flex flex-col gap-1 rounded-xl bg-surface p-4">
        {session ? (
          <>
            <p className="font-semibold">{t('admin.signed_in')}</p>
            <p className="text-muted">
              {until ? t('admin.signed_in_until', { time: until }) : t('admin.signed_in_token')}
            </p>
          </>
        ) : (
          <p className="text-muted">{failed ? t('admin.error.failed') : t('admin.checking')}</p>
        )}
      </div>
      <Cameras />
      <p className="text-muted">{t('admin.coming_soon')}</p>
      <button
        type="button"
        onClick={() => void adminLogout().catch(() => undefined)}
        className="min-h-11 rounded-xl bg-surface px-4 font-semibold"
      >
        {t('admin.log_out')}
      </button>
    </section>
  )
}

export default function AdminScreen() {
  const token = useAdminToken()
  if (!token) return <Login />
  return (
    <Routes>
      <Route path="cameras/:id" element={<CameraDetail />} />
      <Route path="*" element={<AdminHome token={token} />} />
    </Routes>
  )
}
