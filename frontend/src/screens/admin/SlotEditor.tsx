import {
  createEditor,
  type Editor,
  type InDirection,
  type LineTarget,
  SLOT_TYPES,
} from '@slot-editor'
import { useEffect, useReducer, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router'
import {
  ApiRequestError,
  type CameraConfigKind,
  type ConfigSaved,
  getCameraConfig,
  getCameraSnapshot,
  putCameraConfig,
} from '../../api/client'
import { useAdminCameras } from '../../hooks/useAdminCameras'
import ReferenceFrame from './ReferenceFrame'

// `#/admin/cameras/<id>/edit` (P7.3, frontend.md §2.4): the slot editor core from
// tools/slot-editor (`@slot-editor`) on the camera's current plain snapshot, with the stored
// slot file (occupancy cameras) or line file (flow cameras). Tap adds a corner, drag moves a
// handle, two fingers pan and zoom; "Move all" shifts every shape for a nudged camera.
// Save → PUT (the API validates, keeps a .bak, reloads the worker) → offer a new reference frame.

type Load = { state: 'loading' } | { state: 'ready' } | { state: 'error'; message: string }
type Save =
  | { state: 'idle' | 'saving' }
  | { state: 'saved'; result: ConfigSaved }
  | { state: 'error'; message: string }

const BUTTON = 'min-h-11 rounded-xl bg-surface px-3 font-semibold disabled:opacity-50'
const FIELD = 'min-h-11 rounded-xl bg-surface px-3'

export default function SlotEditor() {
  const { t } = useTranslation()
  const { id = '' } = useParams()
  const { cameras, failed } = useAdminCameras()
  const camera = cameras?.find((c) => c.id === id)
  const kind: CameraConfigKind | null = camera ? (camera.role === 'flow' ? 'lines' : 'slots') : null
  const zones = camera?.zones.join(',') ?? ''

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [, redraw] = useReducer((n: number) => n + 1, 0)
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [save, setSave] = useState<Save>({ state: 'idle' })
  const [dirty, setDirty] = useState(false)
  const [hint, setHint] = useState('')

  // Build the editor once the camera's role is known, then load the picture and the file.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !kind) return
    let ready = false
    const controller = new AbortController()
    const ed = createEditor(canvas, {
      cameraId: id,
      zone: zones.split(',')[0],
      onChange: () => {
        if (ready) setDirty(true)
        redraw()
      },
      onStatus: setHint,
    })
    setEditor(ed)
    setLoad({ state: 'loading' })
    Promise.all([
      getCameraSnapshot(id, { annotated: false, signal: controller.signal }),
      getCameraConfig(id, kind, { signal: controller.signal }).catch((err: unknown) => {
        // no file yet: start from an empty one
        if (err instanceof ApiRequestError && err.error.status === 404) return null
        throw err
      }),
    ])
      .then(async ([picture, file]) => {
        await ed.loadImage(picture, `${id}.jpg`)
        if (file) {
          if (kind === 'slots') ed.loadSlotFile(file)
          else ed.loadLineFile(file)
        }
        ed.setCameraId(id)
        ed.setMode(kind)
        ready = true
        setLoad({ state: 'ready' })
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        const code = err instanceof ApiRequestError ? err.error.code : 'failed'
        const message =
          code === 'unavailable' ? t('admin.camera.unavailable') : t('admin.editor.load_failed')
        setLoad({ state: 'error', message })
      })
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)) return
      if (ready && ed.handleKey(e)) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      controller.abort()
      window.removeEventListener('keydown', onKey)
      ed.destroy()
    }
  }, [id, kind, zones, t])

  const onSave = () => {
    if (!editor || !kind) return
    const errors = kind === 'slots' ? editor.validate() : []
    let file: Record<string, unknown> | null = null
    try {
      if (!errors.length) file = kind === 'slots' ? editor.slotFile() : editor.lineFile()
    } catch (err) {
      errors.push(err instanceof Error ? err.message : String(err))
    }
    if (!file) {
      setSave({ state: 'error', message: errors.join('; ') })
      return
    }
    setSave({ state: 'saving' })
    putCameraConfig(id, kind, file).then(
      (result) => {
        setSave({ state: 'saved', result })
        setDirty(false)
      },
      (err: unknown) => {
        const message =
          err instanceof ApiRequestError && err.error.code === 'bad_request'
            ? t('admin.editor.rejected', { reason: err.error.message })
            : t('admin.editor.save_failed')
        setSave({ state: 'error', message })
      },
    )
  }

  const st = editor?.state
  const ready = load.state === 'ready' && !!st
  const selected = ready ? editor.selectedSlot() : null
  const drawing = ready && st.draft.length > 0

  return (
    <section className="flex flex-col gap-3 py-8">
      <Link to=".." relative="path" className="text-accent">
        {t('admin.editor.back', { id })}
      </Link>
      <h1 className="text-2xl font-bold">
        {t(kind === 'lines' ? 'admin.editor.title_lines' : 'admin.editor.title_slots', { id })}
      </h1>
      {cameras !== null && !camera && <p className="text-muted">{t('admin.camera.not_found')}</p>}
      {failed && <p className="text-muted">{t('admin.cameras.failed')}</p>}
      <p className="text-muted">
        {t(kind === 'lines' ? 'admin.editor.help_lines' : 'admin.editor.help_slots')}
      </p>

      <div className="relative">
        <canvas
          ref={canvasRef}
          role="img"
          aria-label={t('admin.editor.canvas', { id })}
          className="block h-[60vh] w-full rounded-xl bg-surface"
        />
        {load.state !== 'ready' && kind && (
          <p
            role={load.state === 'error' ? 'alert' : 'status'}
            className="absolute inset-x-4 top-4 rounded-xl bg-bg/90 p-3"
          >
            {load.state === 'error' ? load.message : t('admin.editor.loading')}
          </p>
        )}
      </div>
      <p aria-live="polite" className="min-h-6 text-sm text-muted">
        {hint}
      </p>

      {ready && (
        <>
          <div className="flex flex-wrap gap-2">
            <label className={`${FIELD} flex items-center gap-2`}>
              <input
                type="checkbox"
                checked={st.moveAll}
                onChange={(e) => editor.setMoveAll(e.target.checked)}
                className="size-5"
              />
              <span>{t('admin.editor.move_all')}</span>
            </label>
            <button type="button" className={BUTTON} onClick={() => editor.fit()}>
              {t('admin.editor.fit')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => editor.finish()}
              disabled={!drawing}
            >
              {t('admin.editor.finish')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => editor.undoPoint()}
              disabled={!drawing}
            >
              {t('admin.editor.undo')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => editor.cancel()}
              disabled={!drawing && !selected}
            >
              {t('admin.editor.cancel')}
            </button>
          </div>

          {kind === 'slots' && camera && camera.zones.length > 1 && (
            <label className="flex items-center gap-2">
              <span>{t('admin.editor.zone')}</span>
              <select
                className={FIELD}
                value={st.zone}
                onChange={(e) => editor.setZone(e.target.value)}
              >
                {camera.zones.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            </label>
          )}

          {kind === 'slots' && selected && (
            <fieldset className="flex flex-wrap items-center gap-2 rounded-xl bg-surface p-3">
              <legend className="font-semibold">
                {t('admin.editor.selected', { id: selected.id })}
              </legend>
              <label className="flex items-center gap-2">
                <span>{t('admin.editor.slot_id')}</span>
                <input
                  key={`${st.selected}-${selected.id}`}
                  defaultValue={selected.id}
                  onBlur={(e) => {
                    const value = e.target.value.trim()
                    if (value && value !== selected.id) editor.updateSelected({ id: value })
                    else e.target.value = selected.id
                  }}
                  className={`${FIELD} w-24 bg-bg`}
                />
              </label>
              <label className="flex items-center gap-2">
                <span>{t('admin.editor.slot_type')}</span>
                <select
                  value={selected.type}
                  onChange={(e) => editor.updateSelected({ type: e.target.value })}
                  className={`${FIELD} bg-bg`}
                >
                  {SLOT_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {t(`admin.editor.types.${type}`)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className={`${BUTTON} bg-bg`}
                onClick={() => editor.duplicateSelected('right')}
              >
                {t('admin.editor.duplicate')}
              </button>
              <button
                type="button"
                className={`${BUTTON} bg-bg`}
                onClick={() => editor.deleteSelected()}
              >
                {t('admin.editor.delete')}
              </button>
            </fieldset>
          )}

          {kind === 'lines' && (
            <div className="flex flex-wrap gap-2">
              <label className="flex items-center gap-2">
                <span>{t('admin.editor.draw')}</span>
                <select
                  value={st.lineTarget}
                  onChange={(e) => editor.setLineTarget(e.target.value as LineTarget)}
                  className={FIELD}
                >
                  <option value="line_a">{t('admin.editor.line_a')}</option>
                  <option value="line_b">{t('admin.editor.line_b')}</option>
                  <option value="roi">{t('admin.editor.roi')}</option>
                </select>
              </label>
              <label className="flex items-center gap-2">
                <span>{t('admin.editor.in_direction')}</span>
                <select
                  value={st.lines.inDirection}
                  onChange={(e) => editor.setInDirection(e.target.value as InDirection)}
                  className={FIELD}
                >
                  <option value="a_to_b">{t('admin.editor.a_to_b')}</option>
                  <option value="b_to_a">{t('admin.editor.b_to_a')}</option>
                </select>
              </label>
            </div>
          )}

          {kind === 'slots' && (
            <p className="text-muted">{t('admin.editor.count', { count: st.slots.length })}</p>
          )}
          <button
            type="button"
            onClick={onSave}
            disabled={save.state === 'saving'}
            className="min-h-11 rounded-xl bg-accent px-4 font-semibold text-bg disabled:opacity-60"
          >
            {save.state === 'saving' ? t('admin.editor.saving') : t('admin.editor.save')}
          </button>
          {dirty && save.state !== 'saving' && (
            <p className="text-muted">{t('admin.editor.unsaved')}</p>
          )}
          {save.state === 'error' && (
            <p role="alert" className="rounded-xl bg-surface p-4">
              {save.message}
            </p>
          )}
          {save.state === 'saved' && (
            <div role="status" className="flex flex-col gap-3 rounded-xl bg-surface p-4">
              <p className="font-semibold">
                {t('admin.editor.saved', { file: save.result.saved })}
              </p>
              <p className="text-muted">
                {save.result.reloaded
                  ? t('admin.editor.reloaded')
                  : t('admin.editor.not_reloaded', { reason: save.result.message ?? '' })}
              </p>
              <p className="text-muted">{t('admin.editor.reference_hint')}</p>
              <ReferenceFrame cameraId={id} />
            </div>
          )}
        </>
      )}
    </section>
  )
}
