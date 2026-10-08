// Slot / line / label editor core (P1.3). Plain ES module, no build step and no dependencies.
// The file formats are the ones in docs/design/config.md §2–4. The Phase 7 admin imports
// `createEditor` from here, so nothing in this file touches the page outside the canvas.

export const SLOT_TYPES = ['standard', 'accessible', 'ev', 'motorcycle', 'reserved']
export const LABEL_STATES = ['free', 'taken', 'unsure']
export const LINE_TARGETS = ['line_a', 'line_b', 'roi']

const COLORS = {
  free: '#22c55e',
  taken: '#ef4444',
  unsure: '#f59e0b',
  selected: '#3b82f6',
  draft: '#e879f9',
  lineA: '#06b6d4',
  lineB: '#f97316',
  roi: '#facc15',
}
const HIT_PX = 10 // screen pixels for "close to a point"
const DRAG_PX = 4 // screen pixels before a press becomes a drag

// ---------- geometry ----------

export function centroid(poly) {
  let a = 0
  let cx = 0
  let cy = 0
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i]
    const [x1, y1] = poly[(i + 1) % poly.length]
    const cross = x0 * y1 - x1 * y0
    a += cross
    cx += (x0 + x1) * cross
    cy += (y0 + y1) * cross
  }
  if (Math.abs(a) < 1e-9) {
    const n = poly.length
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n]
  }
  return [cx / (3 * a), cy / (3 * a)]
}

export function pointInPolygon([x, y], poly) {
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]
    const [xj, yj] = poly[j]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

function orient(a, b, c) {
  return Math.sign((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
}

function segmentsCross(a, b, c, d) {
  return orient(a, b, c) * orient(a, b, d) < 0 && orient(c, d, a) * orient(c, d, b) < 0
}

/** True if two non-adjacent edges of the closed polygon cross. */
export function isSelfIntersecting(poly) {
  const n = poly.length
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue
      if (segmentsCross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])) return true
    }
  }
  return false
}

function mean(points) {
  return [
    points.reduce((s, p) => s + p[0], 0) / points.length,
    points.reduce((s, p) => s + p[1], 0) / points.length,
  ]
}

/**
 * The offset from one side of a polygon to the opposite side: `right` = mean of the two
 * right-most points minus mean of the two left-most; `down` the same with y. For a
 * four-corner space that is the vector between the midpoints of opposite edges, so a
 * copy shifted by it shares the edge with the original.
 */
export function sideOffset(poly, dir = 'right') {
  const k = dir === 'right' ? 0 : 1
  const sorted = [...poly].sort((p, q) => p[k] - q[k])
  const lo = mean(sorted.slice(0, 2))
  const hi = mean(sorted.slice(-2))
  return [Math.round(hi[0] - lo[0]), Math.round(hi[1] - lo[1])]
}

// ---------- slots ----------

/** Next free id for a zone: zone letter + two digits (G01, G02, … / U01 …). */
export function nextSlotId(slots, zone) {
  const prefix = (zone || 'x')[0].toUpperCase()
  const re = new RegExp(`^${prefix}(\\d+)$`)
  let max = 0
  for (const s of slots) {
    const m = re.exec(s.id)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return `${prefix}${String(max + 1).padStart(2, '0')}`
}

/** A copy of `slot` shifted by its own width (`right`) or depth (`down`), with the next id. */
export function duplicateSlot(slot, slots, dir = 'right') {
  const [dx, dy] = sideOffset(slot.polygon, dir)
  return {
    id: nextSlotId(slots, slot.zone),
    zone: slot.zone,
    polygon: slot.polygon.map(([x, y]) => [x + dx, y + dy]),
    type: slot.type,
  }
}

/** Problems that `load_slots()` would reject, as human-readable strings. */
export function validateSlots(slots) {
  const errors = []
  const seen = new Set()
  for (const s of slots) {
    if (!s.id) errors.push('a slot has no id')
    if (seen.has(s.id)) errors.push(`duplicate slot id ${s.id}`)
    seen.add(s.id)
    if (s.polygon.length < 3) errors.push(`${s.id}: fewer than 3 points`)
    else if (isSelfIntersecting(s.polygon)) errors.push(`${s.id}: polygon crosses itself`)
    if (!SLOT_TYPES.includes(s.type)) errors.push(`${s.id}: unknown type ${s.type}`)
  }
  return errors
}

// ---------- file formats (config.md §2–4) ----------

const roundPoly = (poly) => poly.map(([x, y]) => [Math.round(x), Math.round(y)])

function requireSize(size) {
  if (!Array.isArray(size) || size.length !== 2 || !size.every((v) => v > 0)) {
    throw new Error('image_size must be [width, height]')
  }
  return [size[0], size[1]]
}

function requirePoly(poly, what, min = 3) {
  if (!Array.isArray(poly) || poly.length < min || !poly.every((p) => p.length === 2)) {
    throw new Error(`${what} needs at least ${min} [x, y] points`)
  }
  return poly.map(([x, y]) => [Number(x), Number(y)])
}

export function toSlotFile({ cameraId, imageSize, referenceImage, slots, countZones }) {
  const out = { version: 1, camera_id: cameraId, image_size: requireSize(imageSize) }
  if (referenceImage) out.reference_image = referenceImage
  out.slots = slots.map((s) => ({
    id: s.id,
    zone: s.zone,
    polygon: roundPoly(s.polygon),
    type: s.type || 'standard',
  }))
  out.count_zones = (countZones || []).map((z) => ({ zone: z.zone, polygon: roundPoly(z.polygon) }))
  return out
}

export function parseSlotFile(data) {
  if (data.version !== 1) throw new Error('slot file: version must be 1')
  return {
    cameraId: data.camera_id,
    imageSize: requireSize(data.image_size),
    referenceImage: data.reference_image ?? null,
    slots: (data.slots || []).map((s) => ({
      id: s.id,
      zone: s.zone,
      polygon: requirePoly(s.polygon, `slot ${s.id}`),
      type: s.type || 'standard',
    })),
    countZones: (data.count_zones || []).map((z) => ({
      zone: z.zone,
      polygon: requirePoly(z.polygon, `count zone ${z.zone}`),
    })),
  }
}

export function toLineFile({ cameraId, imageSize, roi, lineA, lineB, inDirection }) {
  if (!lineA || !lineB) throw new Error('draw line_a and line_b first')
  const out = { version: 1, camera_id: cameraId, image_size: requireSize(imageSize) }
  if (roi) out.roi = roundPoly(roi)
  out.line_a = roundPoly(lineA)
  out.line_b = roundPoly(lineB)
  out.in_direction = inDirection || 'a_to_b'
  return out
}

export function parseLineFile(data) {
  if (data.version !== 1) throw new Error('line file: version must be 1')
  return {
    cameraId: data.camera_id,
    imageSize: requireSize(data.image_size),
    roi: data.roi ? requirePoly(data.roi, 'roi') : null,
    lineA: requirePoly(data.line_a, 'line_a', 2).slice(0, 2),
    lineB: requirePoly(data.line_b, 'line_b', 2).slice(0, 2),
    inDirection: data.in_direction || 'a_to_b',
  }
}

/** free → taken → unsure → free */
export function cycleLabel(state) {
  return LABEL_STATES[(LABEL_STATES.indexOf(state) + 1) % LABEL_STATES.length]
}

/** Labels for one image as { slotId: 'taken' | 'unsure' } (free slots are left out). */
export function imageLabelState(entry) {
  const out = {}
  for (const id of entry?.taken || []) out[id] = 'taken'
  for (const id of entry?.unsure || []) out[id] = 'unsure'
  return out
}

/** Set one slot's label in an images-map entry, keeping `taken`/`unsure` in slot order. */
export function setLabel(entry, slotId, state, slotOrder) {
  const states = imageLabelState(entry)
  if (state === 'free') delete states[slotId]
  else states[slotId] = state
  const order = (id) => {
    const i = slotOrder.indexOf(id)
    return i < 0 ? Infinity : i
  }
  const pick = (want) =>
    Object.keys(states)
      .filter((id) => states[id] === want)
      .sort((a, b) => order(a) - order(b) || a.localeCompare(b))
  return { conditions: entry?.conditions || [], taken: pick('taken'), unsure: pick('unsure') }
}

export function toLabelsFile({ cameraId, images }) {
  const out = { version: 1, camera_id: cameraId, images: {} }
  for (const name of Object.keys(images).sort()) {
    const e = images[name]
    out.images[name] = {
      conditions: [...(e.conditions || [])],
      taken: [...(e.taken || [])],
      unsure: [...(e.unsure || [])],
    }
  }
  return out
}

export function parseLabelsFile(data) {
  if (data.version !== 1) throw new Error('labels file: version must be 1')
  return { cameraId: data.camera_id, images: toLabelsFile(data).images }
}

/**
 * JSON in the layout used by the specs: one key per line, every slot / count zone / image on
 * its own line, point lists inline. Exporting an imported file gives the same text back.
 */
export function formatJson(value) {
  const inline = (v) => {
    if (Array.isArray(v)) return `[${v.map(inline).join(', ')}]`
    if (v && typeof v === 'object') {
      if (Object.keys(v).length === 0) return '{}'
      return `{ ${Object.keys(v)
        .map((k) => `${JSON.stringify(k)}: ${inline(v[k])}`)
        .join(', ')} }`
    }
    return JSON.stringify(v)
  }
  const fmt = (v, indent) => {
    if (Array.isArray(v)) {
      if (v.length === 0 || v.every((x) => typeof x !== 'object' || Array.isArray(x))) {
        return inline(v)
      }
      const pad = indent + '  '
      return `[\n${v.map((x) => pad + inline(x)).join(',\n')}\n${indent}]`
    }
    if (v && typeof v === 'object') {
      const pad = indent + '  '
      if (indent.length >= 4) return inline(v) // the per-image entries of a labels file
      const keys = Object.keys(v)
      if (keys.length === 0) return '{}'
      return `{\n${keys.map((k) => `${pad}${JSON.stringify(k)}: ${fmt(v[k], pad)}`).join(',\n')}\n${indent}}`
    }
    return JSON.stringify(v)
  }
  return fmt(value, '') + '\n'
}

// ---------- the editor ----------

/**
 * createEditor(canvas, opts) → editor
 *
 * opts: { zone: 'ground', cameraId: 'cam-ground', onChange(editor), onStatus(text) }
 * Modes: 'slots' | 'lines' | 'label'. Mouse: click adds points / selects / labels; drag pans,
 * or moves a corner of the selected shape; wheel or pinch zooms. Keys go through
 * `editor.handleKey(event)` so the page decides when the editor gets them.
 */
export function createEditor(canvas, opts = {}) {
  const ctx = canvas.getContext('2d')
  const state = {
    mode: 'slots',
    image: null,
    imageName: null,
    imageSize: null,
    view: { scale: 1, ox: 0, oy: 0 },
    cameraId: opts.cameraId || 'cam-ground',
    zone: opts.zone || 'ground',
    referenceImage: null,
    slots: [],
    countZones: [],
    selected: -1,
    draft: [],
    lines: { roi: null, lineA: null, lineB: null, inDirection: 'a_to_b' },
    lineTarget: 'line_a',
    labelImages: {},
    hover: null,
  }
  const pointers = new Map()
  let press = null // { x, y, kind: 'pan' | 'vertex' | 'pinch', ... }

  const changed = () => {
    draw()
    opts.onChange?.(editor)
  }
  const status = (text) => opts.onStatus?.(text)

  // --- view ---
  const toImage = (sx, sy) => [
    (sx - state.view.ox) / state.view.scale,
    (sy - state.view.oy) / state.view.scale,
  ]
  const toScreen = ([x, y]) => [
    x * state.view.scale + state.view.ox,
    y * state.view.scale + state.view.oy,
  ]
  const hitRadius = () => HIT_PX / state.view.scale

  function resize() {
    const rect = canvas.getBoundingClientRect()
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.max(1, Math.round(rect.width * dpr))
    canvas.height = Math.max(1, Math.round(rect.height * dpr))
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw()
  }

  function fit() {
    if (!state.imageSize) return
    const rect = canvas.getBoundingClientRect()
    const [w, h] = state.imageSize
    const scale = Math.min(rect.width / w, rect.height / h) * 0.98
    state.view = { scale, ox: (rect.width - w * scale) / 2, oy: (rect.height - h * scale) / 2 }
    draw()
  }

  function zoomAt(sx, sy, factor) {
    const scale = Math.min(Math.max(state.view.scale * factor, 0.02), 40)
    const [ix, iy] = toImage(sx, sy)
    state.view = { scale, ox: sx - ix * scale, oy: sy - iy * scale }
    draw()
  }

  // --- drawing ---
  function pathPoly(poly, close = true) {
    ctx.beginPath()
    poly.forEach((p, i) => {
      const [x, y] = toScreen(p)
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    })
    if (close) ctx.closePath()
  }

  function handles(poly, color) {
    ctx.fillStyle = color
    for (const p of poly) {
      const [x, y] = toScreen(p)
      ctx.fillRect(x - 4, y - 4, 8, 8)
    }
  }

  function label(text, [x, y], color) {
    const [sx, sy] = toScreen([x, y])
    ctx.font = 'bold 13px system-ui, sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.lineWidth = 3
    ctx.strokeStyle = 'rgba(0,0,0,0.8)'
    ctx.strokeText(text, sx, sy)
    ctx.fillStyle = color
    ctx.fillText(text, sx, sy)
  }

  function arrow(from, to, color) {
    const [x0, y0] = toScreen(from)
    const [x1, y1] = toScreen(to)
    const a = Math.atan2(y1 - y0, x1 - x0)
    ctx.strokeStyle = color
    ctx.fillStyle = color
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(x0, y0)
    ctx.lineTo(x1, y1)
    ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x1 - 12 * Math.cos(a - 0.4), y1 - 12 * Math.sin(a - 0.4))
    ctx.lineTo(x1 - 12 * Math.cos(a + 0.4), y1 - 12 * Math.sin(a + 0.4))
    ctx.closePath()
    ctx.fill()
  }

  function drawSlots() {
    const labels = state.mode === 'label' ? imageLabelState(currentLabels()) : {}
    state.slots.forEach((s, i) => {
      const selected = state.mode === 'slots' && i === state.selected
      const st = labels[s.id] || 'free'
      const color = selected ? COLORS.selected : COLORS[st]
      pathPoly(s.polygon)
      if (st !== 'free' || selected) {
        ctx.fillStyle = color + '55'
        ctx.fill()
      }
      ctx.lineWidth = selected ? 3 : 2
      ctx.strokeStyle = color
      ctx.stroke()
      label(s.id, centroid(s.polygon), selected ? '#bfdbfe' : '#ffffff')
      if (selected) handles(s.polygon, COLORS.selected)
    })
    for (const z of state.countZones) {
      ctx.setLineDash([6, 4])
      pathPoly(z.polygon)
      ctx.strokeStyle = '#a3a3a3'
      ctx.lineWidth = 1.5
      ctx.stroke()
      ctx.setLineDash([])
    }
  }

  function drawLines() {
    const { roi, lineA, lineB, inDirection } = state.lines
    if (roi) {
      pathPoly(roi)
      ctx.fillStyle = COLORS.roi + '22'
      ctx.fill()
      ctx.strokeStyle = COLORS.roi
      ctx.lineWidth = 2
      ctx.stroke()
      handles(roi, COLORS.roi)
    }
    for (const [line, color, name] of [
      [lineA, COLORS.lineA, 'A'],
      [lineB, COLORS.lineB, 'B'],
    ]) {
      if (!line) continue
      pathPoly(line, false)
      ctx.strokeStyle = color
      ctx.lineWidth = 3
      ctx.stroke()
      handles(line, color)
      label(name, line[0], color)
    }
    if (lineA && lineB) {
      const [a, b] = [mean(lineA), mean(lineB)]
      const [from, to] = inDirection === 'a_to_b' ? [a, b] : [b, a]
      arrow(from, to, '#ffffff')
      label('IN', mean([from, to]), '#ffffff')
    }
  }

  function draw() {
    const rect = canvas.getBoundingClientRect()
    ctx.clearRect(0, 0, rect.width, rect.height)
    ctx.fillStyle = '#111827'
    ctx.fillRect(0, 0, rect.width, rect.height)
    if (state.image) {
      const [w, h] = state.imageSize
      const [x, y] = toScreen([0, 0])
      ctx.imageSmoothingEnabled = state.view.scale < 2
      ctx.drawImage(state.image, x, y, w * state.view.scale, h * state.view.scale)
    }
    if (state.mode === 'lines') drawLines()
    else drawSlots()
    if (state.draft.length) {
      pathPoly(state.draft, false)
      if (state.hover) {
        const [hx, hy] = toScreen(state.hover)
        ctx.lineTo(hx, hy)
      }
      ctx.strokeStyle = COLORS.draft
      ctx.lineWidth = 2
      ctx.stroke()
      handles(state.draft, COLORS.draft)
      const [fx, fy] = toScreen(state.draft[0])
      ctx.strokeStyle = '#ffffff'
      ctx.strokeRect(fx - 7, fy - 7, 14, 14)
    }
  }

  // --- editing helpers ---
  const near = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]) <= hitRadius()
  const clampPt = ([x, y]) => {
    const [w, h] = state.imageSize || [Infinity, Infinity]
    return [
      Math.round(Math.min(Math.max(x, 0), w - 1)),
      Math.round(Math.min(Math.max(y, 0), h - 1)),
    ]
  }

  /** The nearest existing slot corner within the hit radius, else the clamped point. */
  function snap(p) {
    let best = null
    let bestD = hitRadius()
    for (const s of state.slots) {
      for (const q of s.polygon) {
        const d = Math.hypot(p[0] - q[0], p[1] - q[1])
        if (d <= bestD) [best, bestD] = [q, d]
      }
    }
    return best ? [...best] : clampPt(p)
  }

  function slotAt(p) {
    for (let i = state.slots.length - 1; i >= 0; i--) {
      if (pointInPolygon(p, state.slots[i].polygon)) return i
    }
    return -1
  }

  /** Editable point lists in the current mode, as [list, ownerKey]. */
  function editablePolys() {
    if (state.mode === 'slots') {
      return state.selected >= 0 ? [[state.slots[state.selected].polygon, 'slot']] : []
    }
    if (state.mode === 'lines') {
      return ['lineA', 'lineB', 'roi'].filter((k) => state.lines[k]).map((k) => [state.lines[k], k])
    }
    return []
  }

  function vertexAt(p) {
    for (const [poly] of editablePolys()) {
      const i = poly.findIndex((q) => near(p, q))
      if (i >= 0) return { poly, index: i }
    }
    return null
  }

  function finishDraft() {
    const pts = state.draft
    if (state.mode === 'slots') {
      if (pts.length < 3) return status('A space needs at least 3 points')
      if (isSelfIntersecting(pts)) return status('That polygon crosses itself: Esc and redraw')
      const slot = {
        id: nextSlotId(state.slots, state.zone),
        zone: state.zone,
        polygon: pts,
        type: 'standard',
      }
      state.slots.push(slot)
      state.selected = state.slots.length - 1
      status(`Added ${slot.id}`)
    } else if (state.mode === 'lines') {
      if (state.lineTarget === 'roi') {
        if (pts.length < 3) return status('The ROI needs at least 3 points')
        if (isSelfIntersecting(pts)) return status('That polygon crosses itself: Esc and redraw')
        state.lines.roi = pts
      } else {
        if (pts.length !== 2) return
        state.lines[state.lineTarget === 'line_a' ? 'lineA' : 'lineB'] = pts
      }
      status(`Set ${state.lineTarget}`)
    }
    state.draft = []
    changed()
  }

  function cancel() {
    if (state.draft.length) state.draft = []
    else state.selected = -1
    changed()
  }

  function clickAt(p) {
    if (!state.image) return status('Load an image first')
    if (state.mode === 'label') {
      const i = slotAt(p)
      if (i < 0 || !state.imageName) return
      const id = state.slots[i].id
      const entry = currentLabels()
      const next = cycleLabel(imageLabelState(entry)[id] || 'free')
      state.labelImages[state.imageName] = setLabel(
        entry,
        id,
        next,
        state.slots.map((s) => s.id),
      )
      status(`${id}: ${next}`)
      return changed()
    }
    // Slot corners snap to existing corners, so neighbouring spaces share their edge exactly.
    const pt = state.mode === 'slots' ? snap(p) : clampPt(p)
    if (state.draft.length) {
      if (state.draft.length >= 3 && near(p, state.draft[0])) return finishDraft()
      state.draft.push(pt)
      if (state.mode === 'lines' && state.lineTarget !== 'roi' && state.draft.length === 2) {
        return finishDraft()
      }
      return draw()
    }
    // A click on an existing corner starts a new space there; inside a space it selects it.
    const onCorner = state.slots.some((s) => s.polygon.some((q) => near(p, q)))
    if (state.mode === 'slots' && !onCorner) {
      const i = slotAt(p)
      if (i >= 0) {
        state.selected = i
        return changed()
      }
      if (state.selected >= 0) {
        state.selected = -1
        return changed()
      }
    }
    state.draft = [pt]
    draw()
  }

  // --- pointer handling (mouse, pen, touch) ---
  function local(e) {
    const rect = canvas.getBoundingClientRect()
    return [e.clientX - rect.left, e.clientY - rect.top]
  }

  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId)
    const s = local(e)
    pointers.set(e.pointerId, s)
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()]
      press = { kind: 'pinch', dist: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: mean([a, b]) }
      return
    }
    const v = e.button === 0 ? vertexAt(toImage(...s)) : null
    press = v
      ? { kind: 'vertex', ...v, start: s, moved: false }
      : { kind: 'pan', start: s, last: s, moved: false, button: e.button }
  })

  canvas.addEventListener('pointermove', (e) => {
    const s = local(e)
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, s)
    if (!press) {
      if (state.draft.length) {
        state.hover = toImage(...s)
        draw()
      }
      return
    }
    if (press.kind === 'pinch') {
      if (pointers.size < 2) return
      const [a, b] = [...pointers.values()]
      const dist = Math.hypot(a[0] - b[0], a[1] - b[1])
      const mid = mean([a, b])
      state.view.ox += mid[0] - press.mid[0]
      state.view.oy += mid[1] - press.mid[1]
      zoomAt(mid[0], mid[1], dist / press.dist)
      press.dist = dist
      press.mid = mid
      return
    }
    if (!press.moved && Math.hypot(s[0] - press.start[0], s[1] - press.start[1]) < DRAG_PX) return
    press.moved = true
    if (press.kind === 'vertex') {
      press.poly[press.index] = clampPt(toImage(...s))
      draw()
    } else {
      state.view.ox += s[0] - press.last[0]
      state.view.oy += s[1] - press.last[1]
      press.last = s
      draw()
    }
  })

  const release = (e) => {
    pointers.delete(e.pointerId)
    if (!press) return
    const p = press
    if (p.kind === 'pinch') {
      if (pointers.size === 0) press = null
      return
    }
    press = null
    if (p.kind === 'vertex' && p.moved) return changed()
    if (!p.moved && e.type === 'pointerup' && (p.kind === 'vertex' || p.button === 0)) {
      clickAt(toImage(...local(e)))
    }
  }
  canvas.addEventListener('pointerup', release)
  canvas.addEventListener('pointercancel', release)
  canvas.addEventListener('contextmenu', (e) => e.preventDefault())
  canvas.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault()
      const [sx, sy] = local(e)
      zoomAt(sx, sy, Math.exp(-e.deltaY * 0.0015))
    },
    { passive: false },
  )
  canvas.style.touchAction = 'none'
  new ResizeObserver(resize).observe(canvas)

  function currentLabels() {
    return state.labelImages[state.imageName] || { conditions: [], taken: [], unsure: [] }
  }

  function checkSize(fileSize, what) {
    if (
      state.imageSize &&
      (fileSize[0] !== state.imageSize[0] || fileSize[1] !== state.imageSize[1])
    ) {
      status(
        `${what} was drawn on ${fileSize.join('×')} but the image is ${state.imageSize.join('×')}: scaled to fit`,
      )
      const [sx, sy] = [state.imageSize[0] / fileSize[0], state.imageSize[1] / fileSize[1]]
      return (poly) => poly && poly.map(([x, y]) => [Math.round(x * sx), Math.round(y * sy)])
    }
    if (!state.imageSize) state.imageSize = fileSize
    return (poly) => poly
  }

  const editor = {
    get state() {
      return state
    },

    async loadImage(src, name) {
      const img = new Image()
      const url = typeof src === 'string' ? src : URL.createObjectURL(src)
      img.src = url
      await img.decode()
      if (typeof src !== 'string') URL.revokeObjectURL(url)
      state.image = img
      state.imageName = name || (typeof src === 'string' ? src.split('/').pop() : src.name)
      state.imageSize = [img.naturalWidth, img.naturalHeight]
      state.draft = []
      fit()
      changed()
    },

    setMode(mode) {
      state.mode = mode
      state.draft = []
      state.selected = -1
      changed()
    },
    setZone(zone) {
      state.zone = zone
      changed()
    },
    setCameraId(id) {
      state.cameraId = id
      changed()
    },
    setLineTarget(target) {
      state.lineTarget = target
      state.draft = []
      changed()
    },
    setInDirection(dir) {
      state.lines.inDirection = dir
      changed()
    },
    setConditions(conditions) {
      if (!state.imageName) return
      state.labelImages[state.imageName] = { ...currentLabels(), conditions }
      changed()
    },
    /** Record the current image as labelled even if every space is free. */
    markLabelled() {
      if (!state.imageName) return
      state.labelImages[state.imageName] = currentLabels()
      changed()
    },
    selectedSlot() {
      return state.slots[state.selected] || null
    },
    select(index) {
      state.selected = index
      changed()
    },
    updateSelected(fields) {
      const s = state.slots[state.selected]
      if (!s) return
      if (
        fields.id !== undefined &&
        fields.id !== s.id &&
        state.slots.some((o) => o.id === fields.id)
      ) {
        return status(`Id ${fields.id} is already used`)
      }
      Object.assign(s, fields)
      changed()
    },
    deleteSelected() {
      if (state.selected < 0) return
      const [gone] = state.slots.splice(state.selected, 1)
      state.selected = -1
      status(`Deleted ${gone.id}`)
      changed()
    },
    duplicateSelected(dir = 'right') {
      const s = state.slots[state.selected]
      if (!s) return
      state.slots.push(duplicateSlot(s, state.slots, dir))
      state.selected = state.slots.length - 1
      status(`Added ${state.slots[state.selected].id}`)
      changed()
    },
    finish: finishDraft,
    cancel,
    fit,
    zoom: (factor) => {
      const rect = canvas.getBoundingClientRect()
      zoomAt(rect.width / 2, rect.height / 2, factor)
    },

    /** Keyboard shortcuts. Returns true if the key was used. */
    handleKey(e) {
      const k = e.key
      if (k === 'Escape') cancel()
      else if (k === 'Enter') finishDraft()
      else if (state.mode === 'slots' && (k === 'Delete' || k === 'Backspace'))
        editor.deleteSelected()
      else if (state.mode === 'slots' && (k === 'd' || k === 'D')) {
        editor.duplicateSelected(e.shiftKey ? 'down' : 'right')
      } else if (k === 'f') fit()
      else if (k === '+' || k === '=') editor.zoom(1.25)
      else if (k === '-') editor.zoom(0.8)
      else return false
      return true
    },

    // --- files ---
    slotFile() {
      if (!state.imageSize) throw new Error('load an image or a slot file first')
      return toSlotFile({
        cameraId: state.cameraId,
        imageSize: state.imageSize,
        referenceImage: state.referenceImage,
        slots: state.slots,
        countZones: state.countZones,
      })
    },
    loadSlotFile(data) {
      const f = parseSlotFile(data)
      const scale = checkSize(f.imageSize, 'The slot file')
      state.cameraId = f.cameraId
      state.referenceImage = f.referenceImage
      state.slots = f.slots.map((s) => ({ ...s, polygon: scale(s.polygon) }))
      state.countZones = f.countZones.map((z) => ({ ...z, polygon: scale(z.polygon) }))
      state.selected = -1
      state.draft = []
      changed()
    },
    setReferenceImage(path) {
      state.referenceImage = path || null
      changed()
    },
    lineFile() {
      if (!state.imageSize) throw new Error('load an image first')
      return toLineFile({ cameraId: state.cameraId, imageSize: state.imageSize, ...state.lines })
    },
    loadLineFile(data) {
      const f = parseLineFile(data)
      const scale = checkSize(f.imageSize, 'The line file')
      state.cameraId = f.cameraId
      state.lines = {
        roi: scale(f.roi),
        lineA: scale(f.lineA),
        lineB: scale(f.lineB),
        inDirection: f.inDirection,
      }
      changed()
    },
    labelsFile() {
      return toLabelsFile({ cameraId: state.cameraId, images: state.labelImages })
    },
    loadLabelsFile(data) {
      state.labelImages = parseLabelsFile(data).images
      changed()
    },
    currentLabels,
    validate: () => validateSlots(state.slots),
    redraw: draw,
  }
  resize()
  return editor
}
