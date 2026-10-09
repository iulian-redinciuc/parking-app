import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiRequestError, saveReferenceFrame } from '../../api/client'

// "Save reference frame" (P7.3, vision.md §6): the worker keeps its current frame as the
// picture camera-shift detection compares against. Offered on the camera page and after the
// slots/lines are saved, since a re-calibrated camera needs a new reference.

type Result = { ok: true } | { ok: false; message: string }

export default function ReferenceFrame({ cameraId }: { cameraId: string }) {
  const { t } = useTranslation()
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null)

  const save = () => {
    setBusy(true)
    setResult(null)
    saveReferenceFrame(cameraId)
      .then(
        (): Result => ({ ok: true }),
        (err: unknown): Result => {
          const code = err instanceof ApiRequestError ? err.error.code : 'failed'
          const key =
            code === 'conflict'
              ? 'admin.reference.no_frame'
              : code === 'unavailable'
                ? 'admin.reference.unavailable'
                : 'admin.reference.failed'
          return { ok: false, message: t(key) }
        },
      )
      .then((r) => {
        setResult(r)
        setBusy(false)
      })
  }

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        onClick={save}
        disabled={busy}
        className="min-h-11 rounded-xl bg-surface px-4 font-semibold disabled:opacity-60"
      >
        {busy ? t('admin.reference.saving') : t('admin.reference.save')}
      </button>
      {result && (
        <p role={result.ok ? 'status' : 'alert'} className="text-muted">
          {result.ok ? t('admin.reference.saved') : result.message}
        </p>
      )}
    </div>
  )
}
