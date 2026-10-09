import type { TFunction } from 'i18next'
import { type FormEvent, useEffect, useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  ApiRequestError,
  type Correction,
  correctZone,
  getCorrections,
  getLot,
} from '../../api/client'
import type { LotZoneInfo, ZoneStatus } from '../../api/types'
import { useLiveStatus } from '../../hooks/useLiveStatus'

// Count corrections (P7.4, frontend.md §2.4): per entry/exit (`flow`) zone its live count, the
// real number and a note → `POST /api/admin/zones/{id}/correct`; the API publishes the new status
// to every app over SSE. Below it the last 50 corrections (who, when, old → new, note).
// The zones come from `/api/lot`, so the first count can be set before the API has any data
// (`/api/status` is 503 until then).

const LOG_LIMIT = 50
const NOTE_MAX = 200

function ZoneCorrection({
  zone,
  live,
  onSaved,
}: {
  zone: LotZoneInfo
  /** The zone's live status; `undefined` before the API has data. */
  live: ZoneStatus | undefined
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const id = useId()
  const [value, setValue] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  async function save(event: FormEvent) {
    event.preventDefault()
    const occupied = Number(value)
    if (!Number.isInteger(occupied) || occupied < 0 || occupied > zone.capacity) {
      setMessage({ text: t('admin.zones.invalid', { capacity: zone.capacity }), error: true })
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      await correctZone(zone, occupied, note)
      setMessage({
        text: t('admin.zones.saved', { zone: zone.name, value: occupied }),
        error: false,
      })
      setValue('')
      setNote('')
      onSaved()
    } catch (err) {
      if (err instanceof ApiRequestError && err.error.status === 401) return // back to login
      const rejected =
        err instanceof ApiRequestError && [404, 409, 422].includes(err.error.status ?? 0)
      setMessage({
        text: rejected
          ? t('admin.zones.rejected', { reason: (err as ApiRequestError).error.message })
          : t('admin.zones.failed'),
        error: true,
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      noValidate
      onSubmit={(e) => void save(e)}
      aria-labelledby={`${id}-name`}
      className="flex flex-col gap-3 rounded-xl bg-surface p-4"
    >
      <p>
        <span id={`${id}-name`} className="font-semibold">
          {zone.name}
        </span>
        {live?.stale && <span className="text-muted"> · {t('admin.zones.stale')}</span>}
      </p>
      <p className="tabular-nums text-muted">
        {live
          ? t('admin.zones.current', {
              occupied: live.occupied,
              capacity: live.capacity,
              free: live.free,
            })
          : t('admin.zones.no_count', { capacity: zone.capacity })}
      </p>
      <label className="flex flex-col gap-1">
        <span>{t('admin.zones.real_count')}</span>
        <input
          type="number"
          inputMode="numeric"
          min={0}
          max={zone.capacity}
          step={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="min-h-11 rounded-xl border border-muted/40 bg-bg px-3 tabular-nums"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span>{t('admin.zones.note')}</span>
        <input
          type="text"
          maxLength={NOTE_MAX}
          value={note}
          placeholder={t('admin.zones.note_placeholder')}
          onChange={(e) => setNote(e.target.value)}
          className="min-h-11 rounded-xl border border-muted/40 bg-bg px-3"
        />
      </label>
      <button
        type="submit"
        disabled={busy || value.trim() === ''}
        className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
      >
        {busy ? t('admin.zones.saving') : t('admin.zones.save')}
      </button>
      {message && (
        <p role={message.error ? 'alert' : 'status'} className={message.error ? 'text-bad' : ''}>
          {message.text}
        </p>
      )}
    </form>
  )
}

function actorLabel(actor: string, t: TFunction): string {
  if (actor === 'admin-token') return t('admin.corrections.actor.token')
  if (actor === 'scheduled-reset') return t('admin.corrections.actor.reset')
  const session = /^session:(.+)$/.exec(actor)
  return session ? t('admin.corrections.actor.session', { id: session[1] }) : actor
}

function CorrectionsLog({ version }: { version: number }) {
  const { t, i18n } = useTranslation()
  const [log, setLog] = useState<Correction[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    getCorrections(LOG_LIMIT, { signal: controller.signal }).then(
      (list) => {
        setLog(list)
        setFailed(false)
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        if (!(err instanceof ApiRequestError && err.error.status === 401)) setFailed(true)
      },
    )
    return () => controller.abort()
  }, [version])

  const when = new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' })
  return (
    <section aria-labelledby="admin-corrections" className="flex flex-col gap-2">
      <h2 id="admin-corrections" className="text-lg font-semibold">
        {t('admin.corrections.title')}
      </h2>
      {failed && (
        <p role="alert" className="rounded-xl bg-surface p-4">
          {t('admin.corrections.failed')}
        </p>
      )}
      {log === null ? (
        !failed && <p className="text-muted">{t('admin.corrections.loading')}</p>
      ) : log.length === 0 ? (
        <p className="text-muted">{t('admin.corrections.empty')}</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {log.map((c) => (
            <li key={c.id} className="flex flex-col gap-0.5 rounded-xl bg-surface p-3 text-sm">
              <span className="font-semibold tabular-nums">
                {t('admin.corrections.entry', {
                  zone: c.zone_name,
                  old: c.old_occupied,
                  new: c.new_occupied,
                })}
              </span>
              <span className="text-muted">
                <time dateTime={c.ts}>{when.format(new Date(c.ts))}</time>
                {' · '}
                {actorLabel(c.actor, t)}
              </span>
              {c.note && <span>{c.note}</span>}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export default function Zones() {
  const { t } = useTranslation()
  const { status } = useLiveStatus()
  const [lotZones, setLotZones] = useState<LotZoneInfo[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    getLot({ signal: controller.signal }).then(
      (lot) => setLotZones(lot.zones),
      () => !controller.signal.aborted && setFailed(true),
    )
    return () => controller.abort()
  }, [])

  // the live status names the zones in the app's language too; `/api/lot` covers "no data yet"
  const zones = (status?.zones ?? lotZones)?.filter((z) => z.method === 'flow')

  return (
    <>
      <section aria-labelledby="admin-zones" className="flex flex-col gap-2">
        <h2 id="admin-zones" className="text-lg font-semibold">
          {t('admin.zones.title')}
        </h2>
        {zones === undefined ? (
          <p className="text-muted">
            {failed ? t('admin.zones.load_failed') : t('admin.zones.loading')}
          </p>
        ) : zones.length === 0 ? (
          <p className="text-muted">{t('admin.zones.none')}</p>
        ) : (
          <>
            <p className="text-muted">{t('admin.zones.intro')}</p>
            {zones.map((zone) => (
              <ZoneCorrection
                key={zone.id}
                zone={zone}
                live={status?.zones.find((z) => z.id === zone.id)}
                onSaved={() => setVersion((v) => v + 1)}
              />
            ))}
          </>
        )}
      </section>
      <CorrectionsLog version={version} />
    </>
  )
}
