import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router'
import { ApiRequestError, getCameraSnapshot } from '../../api/client'
import { useAdminCameras } from '../../hooks/useAdminCameras'
import { CameraHealth } from './Cameras'

// One camera (P7.2, frontend.md §2.4): its health and a live snapshot, annotated with the
// worker's last analysis by default. The JPEG comes with the bearer header and is shown through
// a blob URL (an `<img src>` can't send headers); each new picture revokes the previous URL.

interface Shot {
  key: string
  url: string | null
  error?: string
}

export default function CameraDetail() {
  const { t } = useTranslation()
  const { id = '' } = useParams()
  const { cameras, failed } = useAdminCameras()
  const camera = cameras?.find((c) => c.id === id)
  const [annotated, setAnnotated] = useState(true)
  const [request, setRequest] = useState(0)
  // the latest finished snapshot; it's loading while its key isn't the current one
  const [shot, setShot] = useState<Shot | null>(null)
  const key = `${id}|${annotated}|${request}`
  const busy = shot?.key !== key
  const image = shot?.url ?? null
  const error = busy ? null : (shot?.error ?? null)

  useEffect(() => {
    const controller = new AbortController()
    getCameraSnapshot(id, { annotated, signal: controller.signal }).then(
      (blob) => {
        const url = URL.createObjectURL(blob)
        setShot({ key, url })
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        const code = err instanceof ApiRequestError ? err.error.code : 'failed'
        const message =
          code === 'unavailable' ? t('admin.camera.unavailable') : t('admin.camera.failed')
        setShot((prev) => ({ key, url: prev?.url ?? null, error: message }))
      },
    )
    return () => controller.abort()
  }, [id, annotated, key, t])

  useEffect(() => {
    return () => {
      if (image) URL.revokeObjectURL(image)
    }
  }, [image])

  return (
    <section className="flex flex-col gap-4 py-8">
      <Link to="../.." relative="path" className="text-accent">
        {t('admin.camera.back')}
      </Link>
      <h1 className="text-2xl font-bold">{id}</h1>
      {camera ? (
        <div className="rounded-xl bg-surface p-4">
          <CameraHealth camera={camera} />
        </div>
      ) : (
        cameras !== null && <p className="text-muted">{t('admin.camera.not_found')}</p>
      )}
      {failed && <p className="text-muted">{t('admin.cameras.failed')}</p>}
      <figure className="flex flex-col gap-2">
        {image ? (
          <img
            src={image}
            alt={t(annotated ? 'admin.camera.snapshot_annotated' : 'admin.camera.snapshot', { id })}
            className={`w-full rounded-xl ${busy ? 'opacity-60' : ''}`}
          />
        ) : (
          busy && <p className="text-muted">{t('admin.camera.loading')}</p>
        )}
        {error && (
          <p role="alert" className="rounded-xl bg-surface p-4">
            {error}
          </p>
        )}
      </figure>
      <label className="flex min-h-11 items-center gap-3">
        <input
          type="checkbox"
          checked={annotated}
          onChange={(e) => setAnnotated(e.target.checked)}
          className="size-5"
        />
        <span>{t('admin.camera.annotated')}</span>
      </label>
      <button
        type="button"
        onClick={() => setRequest((n) => n + 1)}
        disabled={busy}
        className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
      >
        {busy ? t('admin.camera.loading') : t('admin.camera.refresh')}
      </button>
    </section>
  )
}
