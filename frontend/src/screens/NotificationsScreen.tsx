import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import {
  getAdminAlerts,
  getAdminToken,
  getLot,
  onAdminTokenChange,
  setAdminAlerts,
  type AdminIssue,
} from '../api/client'
import { IS_MOCK } from '../api/base'
import type { LotInfo } from '../api/types'
import InstallHint from '../components/InstallHint'
import { requestLocation } from '../lib/geo'
import {
  cancelOnMyWay,
  formatCountdown,
  isSubscribed,
  loadPrefs,
  MAX_SCHEDULES,
  ON_MY_WAY_MINUTES,
  onMyWayUntil,
  PREFS_SAVE_DELAY_MS,
  permission,
  PushError,
  pushSupport,
  saveLocalPrefs,
  sendTest,
  startOnMyWay,
  storedEndpoint,
  subscribe,
  unsubscribe,
  updatePrefs,
  type Prefs,
  type Schedule,
} from '../lib/push'

// The Alerts screen (frontend.md §2.2, notifications.md §2): never asks for permission until
// "Enable notifications" is tapped; iPhone Safari outside the installed app gets the install hint.
// Settings save themselves with a PATCH 500 ms after the last change. A logged-in admin also gets
// "Receive admin alerts" for this device's subscription (P7.8).

type View = 'checking' | 'unsupported' | 'ios-not-installed' | 'default' | 'denied' | 'subscribed'

const RADIUS = { min: 200, max: 2000, step: 100 }
const DAYS = [1, 2, 3, 4, 5, 6, 7]
const WORKDAYS = [1, 2, 3, 4, 5]

async function initialView(): Promise<View> {
  const support = pushSupport()
  if (support !== 'supported') return support
  if (permission() === 'denied') return 'denied'
  return (await isSubscribed()) ? 'subscribed' : 'default'
}

function errorKey(err: unknown): string {
  return `alerts.error.${err instanceof PushError ? err.code : 'failed'}`
}

/** "Mon", "Tue"… in the UI language (2024-01-01 was a Monday). */
function dayName(day: number, lang: string): string {
  return new Intl.DateTimeFormat(lang, { weekday: 'short', timeZone: 'UTC' }).format(
    new Date(Date.UTC(2024, 0, day)),
  )
}

export default function NotificationsScreen() {
  const { t } = useTranslation()
  const [view, setView] = useState<View>('checking')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void initialView().then((v) => live && setView(v))
    return () => {
      live = false
    }
  }, [])

  const enable = async () => {
    setBusy(true)
    setMessage(null)
    try {
      await subscribe(loadPrefs())
      setView('subscribed')
    } catch (err) {
      if (err instanceof PushError && err.code === 'denied') setView('denied')
      else setMessage(t(errorKey(err)))
    } finally {
      setBusy(false)
    }
  }

  const turnOff = async () => {
    setBusy(true)
    await unsubscribe()
    setBusy(false)
    setMessage(t('alerts.turned_off'))
    setView('default')
  }

  return (
    <section className="flex flex-col gap-4 py-4" aria-busy={view === 'checking'}>
      <h1 className="text-2xl font-bold">{t('screen.alerts')}</h1>
      <p className="text-muted">{t('alerts.intro')}</p>
      {IS_MOCK && <p className="text-sm text-muted">{t('alerts.mock_note')}</p>}

      {view === 'unsupported' && (
        <p role="status" className="rounded-xl bg-surface p-4">
          {t('alerts.unsupported')}
        </p>
      )}
      {view === 'ios-not-installed' && (
        <>
          <p>{t('alerts.ios_install')}</p>
          <InstallHint always />
        </>
      )}
      {view === 'denied' && (
        <div role="status" className="flex flex-col gap-2 rounded-xl bg-surface p-4">
          <h2 className="font-semibold">{t('alerts.denied_title')}</h2>
          <p className="text-sm">{t('alerts.denied_browser')}</p>
          <p className="text-sm text-muted">{t('alerts.denied_app')}</p>
        </div>
      )}
      {view === 'default' && (
        <button
          type="button"
          onClick={() => void enable()}
          disabled={busy}
          className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
        >
          {busy ? t('alerts.enabling') : t('alerts.enable')}
        </button>
      )}
      {message && (
        <p role="status" className="text-sm">
          {message}
        </p>
      )}
      {view !== 'checking' && view !== 'subscribed' && <LocalNearSettings />}
      {view === 'subscribed' && <PushSettings onGone={() => setView('default')} />}
      {view === 'subscribed' && (
        <button
          type="button"
          onClick={() => void turnOff()}
          disabled={busy}
          className="min-h-11 rounded-xl border border-muted px-4 disabled:opacity-60"
        >
          {t('alerts.turn_off')}
        </button>
      )}
    </section>
  )
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

function PushSettings({ onGone }: { onGone: () => void }) {
  const { t, i18n } = useTranslation()
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs)
  const [lot, setLot] = useState<LotInfo | null>(null)
  const [save, setSave] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [test, setTest] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const pending = useRef<{ prefs: Prefs; timer: ReturnType<typeof setTimeout> } | null>(null)
  const onGoneRef = useRef(onGone)

  useEffect(() => {
    onGoneRef.current = onGone
  })

  useEffect(() => {
    let live = true
    getLot()
      .then((info) => live && setLot(info))
      .catch(() => undefined) // zones/radius default stay hidden without the lot info
    return () => {
      live = false
    }
  }, [])

  // Leaving the screen within the 500 ms still saves the last change.
  useEffect(
    () => () => {
      const p = pending.current
      if (!p) return
      clearTimeout(p.timer)
      void updatePrefs(p.prefs).catch(() => undefined)
    },
    [],
  )

  const flush = async (next: Prefs) => {
    pending.current = null
    setSave('saving')
    try {
      await updatePrefs(next)
      setSave('saved')
      setSaveError(null)
    } catch (err) {
      if (err instanceof PushError && err.code === 'gone') onGoneRef.current()
      setSave('error')
      setSaveError(t(errorKey(err)))
    }
  }

  const change = (patch: Partial<Prefs>) => {
    const next = { ...prefs, ...patch }
    setPrefs(next)
    if (pending.current) clearTimeout(pending.current.timer)
    pending.current = {
      prefs: next,
      timer: setTimeout(() => void flush(next), PREFS_SAVE_DELAY_MS),
    }
  }

  const runTest = async () => {
    setTesting(true)
    setTest(null)
    try {
      await sendTest()
      setTest(t('alerts.test_sent'))
    } catch (err) {
      setTest(t(errorKey(err)))
      if (err instanceof PushError && err.code === 'gone') onGoneRef.current()
    } finally {
      setTesting(false)
    }
  }

  const zones = lot?.zones ?? []
  const zoneOn = (id: string) => prefs.zones === null || prefs.zones.includes(id)
  const toggleZone = (id: string) => {
    const on = zones.filter((z) => z.id !== id && zoneOn(z.id)).map((z) => z.id)
    if (!zoneOn(id)) on.push(id)
    // Every zone ticked = `null`, so zones added to the lot later are included too.
    const all = zones.every((z) => on.includes(z.id))
    change({ zones: all ? null : zones.filter((z) => on.includes(z.id)).map((z) => z.id) })
  }

  return (
    <div className="flex flex-col gap-4">
      <p role="status" className="text-sm text-ok">
        {t('alerts.enabled')}
      </p>

      <OnMyWay onGone={() => onGoneRef.current()} />

      <fieldset className="flex flex-col gap-3 rounded-xl bg-surface p-4">
        <legend className="sr-only">{t('alerts.near_legend')}</legend>
        <NearSettings prefs={prefs} lot={lot} onChange={change} />
        <Toggle
          label={t('alerts.almost_full')}
          hint={t('alerts.almost_full_hint')}
          checked={prefs.alert_when_almost_full}
          onChange={(alert_when_almost_full) => change({ alert_when_almost_full })}
        />
      </fieldset>

      {zones.length > 1 && (
        <fieldset className="flex flex-col gap-2 rounded-xl bg-surface p-4">
          <legend className="float-left mb-1 font-semibold">{t('alerts.zones')}</legend>
          {zones.map((z) => (
            <label key={z.id} className="flex min-h-11 items-center gap-3">
              <input
                type="checkbox"
                checked={zoneOn(z.id)}
                onChange={() => toggleZone(z.id)}
                className="size-5 accent-accent"
              />
              <span>{z.name}</span>
            </label>
          ))}
        </fieldset>
      )}

      <Reminders
        schedules={prefs.schedules}
        lang={i18n.language}
        onChange={(schedules) => change({ schedules })}
      />

      <fieldset className="flex flex-col gap-3 rounded-xl bg-surface p-4">
        <legend className="sr-only">{t('alerts.quiet')}</legend>
        <Toggle
          label={t('alerts.quiet')}
          hint={t('alerts.quiet_hint')}
          checked={prefs.quiet_hours !== null}
          onChange={(on) => change({ quiet_hours: on ? { from: '22:00', to: '07:00' } : null })}
        />
        {prefs.quiet_hours && (
          <div className="flex gap-4">
            <TimeField
              label={t('alerts.quiet_from')}
              value={prefs.quiet_hours.from}
              onChange={(from) =>
                prefs.quiet_hours && change({ quiet_hours: { ...prefs.quiet_hours, from } })
              }
            />
            <TimeField
              label={t('alerts.quiet_to')}
              value={prefs.quiet_hours.to}
              onChange={(to) =>
                prefs.quiet_hours && change({ quiet_hours: { ...prefs.quiet_hours, to } })
              }
            />
          </div>
        )}
      </fieldset>

      <AdminAlertsSetting />

      <p className="min-h-5 text-sm text-muted" aria-live="polite">
        {save === 'saving' && t('alerts.saving')}
        {save === 'saved' && t('alerts.saved')}
        {save === 'error' && <span className="text-bad">{saveError}</span>}
      </p>

      <button
        type="button"
        onClick={() => void runTest()}
        disabled={testing}
        className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
      >
        {testing ? t('alerts.testing') : t('alerts.test')}
      </button>
      {test && (
        <p role="status" className="text-sm">
          {test}
        </p>
      )}
    </div>
  )
}

const clockNow = () => Date.now()

/** "Receive admin alerts" (notifications.md §5.1): only while an admin is logged in on this
 * device; the flag lives on this device's push subscription. Lists the open issues too. */
function AdminAlertsSetting() {
  const { t, i18n } = useTranslation()
  const token = useSyncExternalStore(onAdminTokenChange, getAdminToken, () => null)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [issues, setIssues] = useState<AdminIssue[]>([])
  const [error, setError] = useState<string | null>(null)
  const endpoint = storedEndpoint()

  useEffect(() => {
    if (!token || !endpoint) return
    let live = true
    getAdminAlerts(endpoint)
      .then((a) => {
        if (!live) return
        setEnabled(a.enabled)
        setIssues(a.issues)
      })
      .catch(() => live && setError(t('alerts.admin_error')))
    return () => {
      live = false
    }
  }, [token, endpoint, t])

  if (!token || !endpoint) return null

  const change = async (on: boolean) => {
    setEnabled(on)
    setError(null)
    try {
      setEnabled(await setAdminAlerts(endpoint, on))
    } catch {
      setEnabled(!on)
      setError(t('alerts.admin_error'))
    }
  }

  const time = (iso: string) =>
    new Intl.DateTimeFormat(i18n.language, { hour: '2-digit', minute: '2-digit' }).format(
      new Date(iso),
    )

  return (
    <fieldset className="flex flex-col gap-3 rounded-xl bg-surface p-4">
      <legend className="sr-only">{t('alerts.admin')}</legend>
      <Toggle
        label={t('alerts.admin')}
        hint={t('alerts.admin_hint')}
        checked={enabled === true}
        onChange={(on) => void change(on)}
      />
      {error && (
        <p role="alert" className="text-sm text-bad">
          {error}
        </p>
      )}
      {enabled !== null && (
        <div className="text-sm">
          <h3 className="font-semibold">{t('alerts.admin_issues')}</h3>
          {issues.length === 0 ? (
            <p className="text-muted">{t('alerts.admin_no_issues')}</p>
          ) : (
            <ul className="list-disc pl-5">
              {issues.map((i) => (
                <li key={i.key}>
                  {t(`alerts.admin_issue.${i.kind}`, {
                    subject: i.subject,
                    detail: i.detail ?? '',
                    time: time(i.since),
                  })}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </fieldset>
  )
}

/**
 * *I'm on my way* (Tier 2, notifications.md §4): 15 / 30 / 60 min chips start the server's updates
 * (a push now, then when the numbers change enough, also with the app closed); while it runs a
 * countdown and *Stop updates* (`minutes: 0`) replace the chips.
 */
function OnMyWay({ onGone }: { onGone: () => void }) {
  const { t, i18n } = useTranslation()
  const [until, setUntil] = useState<number | null>(() => onMyWayUntil())
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (until === null) return
    const timer = setInterval(() => {
      const current = Date.now()
      setNow(current)
      if (current >= until) setUntil(null)
    }, 1000)
    return () => clearInterval(timer)
  }, [until])

  const run = async (action: () => Promise<number | null>) => {
    setBusy(true)
    setError(null)
    try {
      const next = await action()
      setNow(clockNow())
      setUntil(next)
    } catch (err) {
      setError(t(errorKey(err)))
      if (err instanceof PushError && err.code === 'gone') onGone()
    } finally {
      setBusy(false)
    }
  }

  const ends = until && new Intl.DateTimeFormat(i18n.language, { timeStyle: 'short' }).format(until)
  return (
    <section aria-labelledby="on-my-way" className="flex flex-col gap-3 rounded-xl bg-surface p-4">
      <h2 id="on-my-way" className="font-semibold">
        {t('alerts.on_my_way')}
      </h2>
      {until === null ? (
        <>
          <p className="text-sm text-muted">{t('alerts.on_my_way_hint')}</p>
          <div className="flex gap-2">
            {ON_MY_WAY_MINUTES.map((minutes) => (
              <button
                key={minutes}
                type="button"
                disabled={busy}
                onClick={() => void run(() => startOnMyWay(minutes))}
                className="min-h-11 flex-1 rounded-full border border-accent px-3 font-semibold text-accent disabled:opacity-60"
              >
                {t('alerts.on_my_way_minutes', { count: minutes })}
              </button>
            ))}
          </div>
        </>
      ) : (
        <div className="flex items-center gap-3">
          <p className="flex flex-1 flex-col">
            <span role="timer" className="text-2xl font-bold tabular-nums">
              {t('alerts.on_my_way_left', { time: formatCountdown(until - now) })}
            </span>
            <span className="text-sm text-muted">
              {t('alerts.on_my_way_until', { time: ends })}
            </span>
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await cancelOnMyWay()
                return null
              })
            }
            className="min-h-11 rounded-xl border border-muted px-4 disabled:opacity-60"
          >
            {t('alerts.on_my_way_cancel')}
          </button>
        </div>
      )}
      {error && (
        <p role="status" className="text-sm text-bad">
          {error}
        </p>
      )}
    </section>
  )
}

/**
 * *Tell me when I'm near* + radius (Tier 1, notifications.md §3). Switching it on asks for the
 * location here, from the tap, never later in the background.
 */
function NearSettings({
  prefs,
  lot,
  onChange,
}: {
  prefs: Prefs
  lot: LotInfo | null
  onChange: (patch: Partial<Prefs>) => void
}) {
  const { t } = useTranslation()
  const [denied, setDenied] = useState(false)
  const toggle = async (on: boolean) => {
    setDenied(false)
    if (on && !(await requestLocation())) {
      setDenied(true)
      return
    }
    onChange({ proximity: on })
  }
  const radius = Math.min(
    RADIUS.max,
    Math.max(RADIUS.min, prefs.radius_m ?? lot?.notify_radius_m ?? 500),
  )
  return (
    <>
      <Toggle
        label={t('alerts.near')}
        hint={t('alerts.near_hint')}
        checked={prefs.proximity}
        onChange={(on) => void toggle(on)}
      />
      {denied && <p className="text-sm text-bad">{t('alerts.near_denied')}</p>}
      {prefs.proximity && (
        <label className="flex flex-col gap-1 text-sm">
          <span>{t('alerts.radius', { meters: radius })}</span>
          <input
            type="range"
            min={RADIUS.min}
            max={RADIUS.max}
            step={RADIUS.step}
            value={radius}
            onChange={(e) => onChange({ radius_m: Number(e.target.value) })}
            className="min-h-11 accent-accent"
          />
        </label>
      )}
    </>
  )
}

/**
 * Without push on this device (not enabled, blocked, unsupported, iPhone Safari outside the app),
 * Tier 1 still works in the open app: the switch is kept on this device only and sent to the
 * server with the rest of the prefs when push is enabled.
 */
function LocalNearSettings() {
  const { t } = useTranslation()
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs)
  const [lot, setLot] = useState<LotInfo | null>(null)
  useEffect(() => {
    let live = true
    getLot()
      .then((info) => live && setLot(info))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [])
  const change = (patch: Partial<Prefs>) => {
    const next = { ...loadPrefs(), ...patch }
    setPrefs(next)
    saveLocalPrefs(next)
  }
  return (
    <fieldset className="flex flex-col gap-3 rounded-xl bg-surface p-4">
      <legend className="sr-only">{t('alerts.near_legend')}</legend>
      <NearSettings prefs={prefs} lot={lot} onChange={change} />
    </fieldset>
  )
}

function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string
  hint: string
  checked: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-4">
      <span className="flex flex-col">
        <span>{label}</span>
        <span className="text-sm text-muted">{hint}</span>
      </span>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-6 shrink-0 accent-accent"
      />
    </label>
  )
}

function TimeField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span>{label}</span>
      <input
        type="time"
        value={value}
        required
        onChange={(e) => e.target.value && onChange(e.target.value)}
        className="min-h-11 rounded-lg border border-muted bg-bg px-2 text-base"
      />
    </label>
  )
}

function Reminders({
  schedules,
  lang,
  onChange,
}: {
  schedules: Schedule[]
  lang: string
  onChange: (schedules: Schedule[]) => void
}) {
  const { t } = useTranslation()
  const [days, setDays] = useState<number[]>(WORKDAYS)
  const [time, setTime] = useState('08:30')
  const full = schedules.length >= MAX_SCHEDULES
  const describe = (s: Schedule) =>
    t('alerts.reminder', { days: s.days.map((d) => dayName(d, lang)).join(', '), time: s.time })

  return (
    <fieldset className="flex flex-col gap-3 rounded-xl bg-surface p-4">
      <legend className="float-left font-semibold">{t('alerts.reminders')}</legend>
      <p className="text-sm text-muted">{t('alerts.reminders_hint')}</p>
      {schedules.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label={t('alerts.reminders')}>
          {schedules.map((s, i) => (
            <li key={`${s.days.join()}-${s.time}-${i}`} className="flex items-center gap-2">
              <span className="flex-1">{describe(s)}</span>
              <button
                type="button"
                onClick={() => onChange(schedules.filter((_, j) => j !== i))}
                aria-label={t('alerts.reminder_remove', { reminder: describe(s) })}
                className="flex size-11 items-center justify-center rounded-full text-muted"
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
            </li>
          ))}
        </ul>
      )}
      <div role="group" aria-label={t('alerts.reminder_days')} className="flex flex-wrap gap-1">
        {DAYS.map((d) => (
          <button
            key={d}
            type="button"
            aria-pressed={days.includes(d)}
            onClick={() =>
              setDays(
                days.includes(d) ? days.filter((x) => x !== d) : [...days, d].sort((a, b) => a - b),
              )
            }
            className="min-h-11 min-w-11 rounded-full border border-muted px-2 text-sm aria-pressed:border-accent aria-pressed:bg-accent aria-pressed:text-bg"
          >
            {dayName(d, lang)}
          </button>
        ))}
      </div>
      <div className="flex items-end gap-3">
        <TimeField label={t('alerts.reminder_time')} value={time} onChange={setTime} />
        <button
          type="button"
          disabled={full || days.length === 0}
          onClick={() => onChange([...schedules, { days, time }])}
          className="min-h-11 flex-1 rounded-xl border border-accent px-4 font-semibold text-accent disabled:opacity-60"
        >
          {t('alerts.reminder_add')}
        </button>
      </div>
      {full && (
        <p className="text-sm text-muted">{t('alerts.reminders_full', { count: MAX_SCHEDULES })}</p>
      )}
    </fieldset>
  )
}
