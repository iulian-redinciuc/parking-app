// Flow tally: the pure parts (events, undo, CSV). The page (index.html) wires them to a
// <video>. CSV format: docs/design/config.md §4 "Flow: data/labels/<clip>.csv", the same one
// `parking evaluate-flow` reads (--truth) and writes (out/eval/flow-<clip>-pred.csv).

export const HEADER = ['video_time_s', 'direction', 'note']
export const DIRECTIONS = ['in', 'out']

/** Events in the order they were added (so undo removes the last key press). */
export function createTally(events = []) {
  let list = []
  let nextId = 1
  const tally = {
    get events() {
      return list.slice()
    },
    add(t, direction, note = '') {
      if (!DIRECTIONS.includes(direction)) throw new Error(`direction must be in or out`)
      if (!Number.isFinite(t) || t < 0) throw new Error('time must be >= 0')
      const ev = { id: nextId++, t: round1(t), direction, note }
      list.push(ev)
      return ev
    },
    /** Remove the last added event; returns it (or null when there is none). */
    undo() {
      return list.pop() ?? null
    },
    remove(id) {
      const before = list.length
      list = list.filter((e) => e.id !== id)
      return list.length < before
    },
    setNote(id, note) {
      const ev = list.find((e) => e.id === id)
      if (ev) ev.note = note
      return Boolean(ev)
    },
    clear() {
      list = []
    },
    /** By time; same time keeps the order they were added. */
    sorted() {
      return list.slice().sort((a, b) => a.t - b.t || a.id - b.id)
    },
    counts() {
      const n = { in: 0, out: 0 }
      for (const e of list) n[e.direction] += 1
      return { ...n, net: n.in - n.out }
    },
  }
  for (const e of events) tally.add(e.t, e.direction, e.note ?? '')
  return tally
}

export function round1(t) {
  return Math.round(t * 10) / 10
}

function quote(field) {
  return /[",\r\n]/.test(field) ? `"${field.replace(/"/g, '""')}"` : field
}

/** Events → CSV text (sorted by time, one decimal, header first). */
export function toCsv(events) {
  const rows = events
    .slice()
    .sort((a, b) => a.t - b.t)
    .map((e) => [round1(e.t).toFixed(1), e.direction, quote(e.note ?? '')].join(','))
  return [HEADER.join(','), ...rows].join('\n') + '\n'
}

/** Split CSV text into rows of fields (RFC 4180 quoting). */
export function splitCsv(text) {
  const rows = []
  let row = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"'
        i++
      } else if (c === '"') quoted = false
      else field += c
    } else if (c === '"') quoted = true
    else if (c === ',') {
      row.push(field)
      field = ''
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else field += c
  }
  if (field !== '' || row.length) {
    row.push(field)
    rows.push(row)
  }
  return rows
}

/** CSV text → events sorted by time; throws on the same problems `parking evaluate-flow` does. */
export function parseCsv(text, name = 'labels') {
  const rows = splitCsv(text.replace(/^\uFEFF/, '')).filter((r) => r.some((c) => c.trim()))
  if (!rows.length) throw new Error(`${name}: empty file (header ${HEADER.join(',')} expected)`)
  const head = rows[0].map((c) => c.trim())
  const ok = head.join(',') === HEADER.join(',') || head.join(',') === HEADER.slice(0, 2).join(',')
  if (!ok) throw new Error(`${name}: header must be ${HEADER.join(',')}, got ${head.join(',')}`)
  const events = rows.slice(1).map((row, i) => {
    const line = i + 2
    if (row.length > head.length || row.length < 2) {
      throw new Error(`${name} line ${line}: expected ${head.length} columns, got ${row.length}`)
    }
    const t = Number(row[0].trim())
    if (row[0].trim() === '' || !Number.isFinite(t) || t < 0) {
      throw new Error(`${name} line ${line}: video_time_s must be a number >= 0`)
    }
    const direction = row[1].trim().toLowerCase()
    if (!DIRECTIONS.includes(direction)) {
      throw new Error(`${name} line ${line}: direction must be in or out, got ${row[1]}`)
    }
    return { t, direction, note: (row[2] ?? '').trim() }
  })
  return events.sort((a, b) => a.t - b.t)
}

/** 125.37 → "2:05.4" (h:mm:ss.s past an hour). */
export function formatTime(t) {
  const tenths = Math.round(t * 10)
  const s = Math.floor(tenths / 10)
  const frac = tenths % 10
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}.${frac}` : `${m}:${ss}.${frac}`
}

/** "rush-2026-10-12.mp4" → "rush-2026-10-12.csv" (the name evaluate-flow looks for). */
export function csvName(videoName) {
  const stem = videoName ? videoName.replace(/\.[^./\\]+$/, '') : 'clip'
  return `${stem || 'clip'}.csv`
}

export const SPEEDS = [1, 2, 3, 4]
