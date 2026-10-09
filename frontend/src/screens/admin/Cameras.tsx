import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { AdminCamera } from '../../api/client'
import { useAdminCameras } from '../../hooks/useAdminCameras'
import { formatAge } from '../../lib/time'

// Camera health (P7.2, frontend.md §2.4): state, issue, fps, last frame age and inference time
// per camera from `GET /api/admin/cameras`, refreshed every 10 s; a row opens the camera detail.

const STATE_TONE: Record<AdminCamera['state'], string> = {
  ok: 'text-ok',
  degraded: 'text-warn',
  down: 'text-bad',
  unknown: 'text-muted',
}

/** The health lines of one camera (list row and detail). */
export function CameraHealth({ camera }: { camera: AdminCamera }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const facts = [
    camera.fps !== null && t('admin.cameras.fps', { value: Math.round(camera.fps * 100) / 100 }),
    camera.last_frame_age_s !== null &&
      t('admin.cameras.frame_age', { age: formatAge(camera.last_frame_age_s, lang) }),
    camera.inference_ms_avg !== null &&
      t('admin.cameras.inference', { ms: Math.round(camera.inference_ms_avg) }),
  ].filter(Boolean)
  return (
    <div className="flex flex-col gap-0.5 text-sm">
      <p>
        <span className={`font-semibold ${STATE_TONE[camera.state]}`}>
          {t(`admin.cameras.state.${camera.state}`)}
        </span>
        {camera.issue && (
          <span>
            {' · '}
            {t(`admin.cameras.issue.${camera.issue}`, { defaultValue: camera.issue })}
          </span>
        )}
      </p>
      {facts.length > 0 && <p className="text-muted tabular-nums">{facts.join(' · ')}</p>}
    </div>
  )
}

export default function Cameras() {
  const { t } = useTranslation()
  const { cameras, failed } = useAdminCameras()
  return (
    <section aria-labelledby="admin-cameras" className="flex flex-col gap-2">
      <h2 id="admin-cameras" className="text-lg font-semibold">
        {t('admin.cameras.title')}
      </h2>
      {failed && (
        <p role="alert" className="rounded-xl bg-surface p-4">
          {t('admin.cameras.failed')}
        </p>
      )}
      {cameras === null ? (
        !failed && <p className="text-muted">{t('admin.cameras.loading')}</p>
      ) : cameras.length === 0 ? (
        <p className="text-muted">{t('admin.cameras.none')}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {cameras.map((camera) => (
            <li key={camera.id}>
              <Link
                to={`cameras/${encodeURIComponent(camera.id)}`}
                className="flex flex-col gap-1 rounded-xl bg-surface p-4"
              >
                <span className="font-semibold">
                  {camera.id}{' '}
                  <span className="font-normal text-muted">
                    {t(`admin.cameras.role.${camera.role}`)}
                  </span>
                </span>
                <CameraHealth camera={camera} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
