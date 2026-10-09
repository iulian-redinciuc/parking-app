import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adminLogin, getCameraConfig, MOCK_ADMIN_PASSWORD, setAdminToken } from '../../api/client'
import AdminScreen from './AdminScreen'

// P7.3 on the mock build: the screen drives the shared editor core, which needs a real canvas,
// so `@slot-editor` is replaced by a fake that records the calls.
const fake = vi.hoisted(() => {
  const state = {
    mode: 'slots',
    zone: 'ground',
    imageSize: null as [number, number] | null,
    slots: [] as { id: string; zone: string; polygon: number[][]; type: string }[],
    selected: -1,
    draft: [],
    lineTarget: 'line_a',
    lines: { roi: null, lineA: null, lineB: null, inDirection: 'a_to_b' },
    moveAll: false,
  }
  let onChange: () => void = () => undefined
  const editor = {
    state,
    errors: [] as string[],
    loadImage: vi.fn(async () => {
      state.imageSize = [640, 360]
    }),
    loadSlotFile: vi.fn((file: { slots: typeof state.slots }) => {
      state.slots = file.slots
    }),
    loadLineFile: vi.fn(),
    setCameraId: vi.fn(),
    setMode: vi.fn(),
    setZone: vi.fn(),
    setMoveAll: vi.fn((on: boolean) => {
      state.moveAll = on
      onChange()
    }),
    setLineTarget: vi.fn(),
    setInDirection: vi.fn(),
    selectedSlot: () => null,
    updateSelected: vi.fn(),
    deleteSelected: vi.fn(),
    duplicateSelected: vi.fn(),
    finish: vi.fn(),
    cancel: vi.fn(),
    undoPoint: vi.fn(),
    fit: vi.fn(),
    zoom: vi.fn(),
    handleKey: vi.fn(() => false),
    validate: vi.fn(() => editor.errors),
    slotFile: vi.fn(() => ({
      version: 1,
      camera_id: 'cam-ground',
      image_size: state.imageSize,
      slots: state.slots.map((s) => ({ ...s, polygon: s.polygon.map(([x, y]) => [x + 7, y]) })),
      count_zones: [],
    })),
    lineFile: vi.fn(),
    destroy: vi.fn(),
  }
  return {
    editor,
    createEditor: vi.fn((_canvas: unknown, opts: { onChange?: () => void }) => {
      onChange = () => opts.onChange?.()
      return editor
    }),
  }
})

vi.mock('@slot-editor', () => ({
  SLOT_TYPES: ['standard', 'accessible', 'ev', 'motorcycle', 'reserved'],
  createEditor: fake.createEditor,
}))

function renderEditor(path = '/admin/cameras/cam-ground/edit') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="admin/*" element={<AdminScreen />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('SlotEditor', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    fake.editor.errors = []
    await adminLogin(MOCK_ADMIN_PASSWORD)
  })
  afterEach(() => {
    setAdminToken(null)
    sessionStorage.clear()
  })

  it('loads the plain snapshot and the slot file into the editor', async () => {
    renderEditor()
    expect(await screen.findByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(fake.editor.loadImage).toHaveBeenCalledWith(expect.any(Blob), 'cam-ground.jpg')
    expect(fake.editor.loadSlotFile).toHaveBeenCalledOnce()
    expect(fake.editor.setMode).toHaveBeenCalledWith('slots')
    expect(screen.getByRole('heading')).toHaveTextContent('Parking spaces: cam-ground')
    expect(screen.getByText('3 spaces')).toBeInTheDocument()
    // the picture was loaded before the file, so the file is scaled to the picture
    const image = fake.editor.loadImage.mock.invocationCallOrder[0]
    expect(image).toBeLessThan(fake.editor.loadSlotFile.mock.invocationCallOrder[0])
  })

  it('move all → save → saved, worker reloaded, then a new reference frame', async () => {
    renderEditor()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Move all' }))
    expect(fake.editor.setMoveAll).toHaveBeenCalledWith(true)
    expect(screen.getByText('Unsaved changes.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await act(async () => {})
    expect(screen.getByText('Saved config/slots/cam-ground.json.')).toBeInTheDocument()
    expect(screen.getByText(/The worker reloaded it/)).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes.')).not.toBeInTheDocument()
    const stored = await getCameraConfig('cam-ground', 'slots')
    expect((stored.slots as { polygon: number[][] }[])[0].polygon[0]).toEqual([67, 220])

    fireEvent.click(screen.getByRole('button', { name: 'Save reference frame' }))
    await act(async () => {})
    expect(screen.getByText(/^Reference frame saved/)).toBeInTheDocument()
  })

  it('an invalid layout is not sent', async () => {
    fake.editor.errors = ['G02: polygon crosses itself']
    renderEditor()
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    expect(screen.getByRole('alert')).toHaveTextContent('G02: polygon crosses itself')
    expect(fake.editor.slotFile).not.toHaveBeenCalled()
  })

  it('a camera without a snapshot says so', async () => {
    renderEditor('/admin/cameras/cam-ramp/edit')
    expect(await screen.findByRole('alert')).toHaveTextContent('No snapshot right now')
    expect(screen.getByRole('heading')).toHaveTextContent('Counting lines: cam-ramp')
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })
})
