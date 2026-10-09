// The admin camera list (P7.2): `GET /api/admin/cameras`, polled every 10 s while mounted.
import { useEffect, useState } from 'react'
import { type AdminCamera, ApiRequestError, getAdminCameras } from '../api/client'

export const CAMERAS_REFRESH_MS = 10_000

/** Polls the camera list every 10 s while mounted; `failed` after an error other than 401. */
export function useAdminCameras(): { cameras: AdminCamera[] | null; failed: boolean } {
  const [cameras, setCameras] = useState<AdminCamera[] | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    const load = () =>
      getAdminCameras({ signal: controller.signal }).then(
        (list) => {
          setCameras(list)
          setFailed(false)
        },
        (err: unknown) => {
          // a 401 already sent the admin back to the login; an abort is the unmount
          if (controller.signal.aborted) return
          if (!(err instanceof ApiRequestError && err.error.status === 401)) setFailed(true)
        },
      )
    void load()
    const timer = setInterval(() => void load(), CAMERAS_REFRESH_MS)
    return () => {
      clearInterval(timer)
      controller.abort()
    }
  }, [])
  return { cameras, failed }
}
