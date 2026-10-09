import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { adminLogin, ApiRequestError, IS_MOCK, MOCK_ADMIN_PASSWORD } from '../../api/client'

// Admin login (api.md §4): password → a 7-day session token in sessionStorage. The parent
// switches to the admin home as soon as the token is stored.

const KNOWN_ERRORS = ['unauthorized', 'rate_limited', 'unavailable']

export default function Login() {
  const { t } = useTranslation()
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!password || busy) return
    setBusy(true)
    setError(null)
    try {
      await adminLogin(password)
    } catch (err) {
      const apiError = err instanceof ApiRequestError ? err.error : null
      const code = apiError && KNOWN_ERRORS.includes(apiError.code) ? apiError.code : 'failed'
      const minutes = Math.max(1, Math.ceil((apiError?.retryAfter ?? 60) / 60))
      setError(t(`admin.error.${code}`, { minutes }))
      setPassword('')
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-4 py-8">
      <h1 className="text-2xl font-bold">{t('admin.title')}</h1>
      <p className="text-muted">{t('admin.login_intro')}</p>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1">
          <span className="font-semibold">{t('admin.password')}</span>
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="min-h-11 rounded-xl border border-muted/40 bg-surface px-3"
          />
        </label>
        <button
          type="submit"
          disabled={busy || !password}
          className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
        >
          {busy ? t('admin.signing_in') : t('admin.sign_in')}
        </button>
      </form>
      {error && (
        <p role="alert" className="rounded-xl bg-surface p-4">
          {error}
        </p>
      )}
      {IS_MOCK && (
        <p className="text-sm text-muted">
          {t('admin.mock_hint', { password: MOCK_ADMIN_PASSWORD })}
        </p>
      )}
    </section>
  )
}
