// Unit tests for tally.js. Run with: node --test tools/flow-tally/
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createTally, csvName, formatTime, parseCsv, splitCsv, toCsv } from './tally.js'

test('add, undo, remove, notes and counts', () => {
  const t = createTally()
  t.add(30.04, 'out')
  const first = t.add(12.36, 'in')
  t.add(12.36, 'in')
  assert.equal(first.t, 12.4)
  assert.deepEqual(t.counts(), { in: 2, out: 1, net: 1 })
  assert.deepEqual(
    t.sorted().map((e) => [e.t, e.direction]),
    [
      [12.4, 'in'],
      [12.4, 'in'],
      [30, 'out'],
    ],
  )
  // undo removes the last key press, not the latest time
  assert.equal(t.undo().id, 3)
  assert.equal(t.events.length, 2)
  assert.ok(t.setNote(first.id, 'van'))
  assert.equal(t.events[1].note, 'van')
  assert.ok(t.remove(1))
  assert.ok(!t.remove(1))
  assert.deepEqual(t.counts(), { in: 1, out: 0, net: 1 })
  t.undo()
  assert.equal(t.undo(), null)
  assert.throws(() => t.add(1, 'sideways'), /in or out/)
  assert.throws(() => t.add(-1, 'in'), />= 0/)
  assert.throws(() => t.add(NaN, 'in'), />= 0/)
})

test('createTally restores events and keeps adding after them', () => {
  const t = createTally([
    { t: 5, direction: 'in', note: 'x' },
    { t: 2, direction: 'out' },
  ])
  assert.equal(t.events.length, 2)
  assert.equal(t.add(9, 'in').id, 3)
  t.clear()
  assert.deepEqual(t.events, [])
})

test('CSV export matches config.md §4 and round-trips', () => {
  const events = [
    { t: 57.94, direction: 'out', note: 'van, white "big"' },
    { t: 12.4, direction: 'in', note: '' },
  ]
  const text = toCsv(events)
  assert.equal(text, 'video_time_s,direction,note\n12.4,in,\n57.9,out,"van, white ""big"""\n')
  assert.deepEqual(parseCsv(text), [
    { t: 12.4, direction: 'in', note: '' },
    { t: 57.9, direction: 'out', note: 'van, white "big"' },
  ])
  assert.equal(toCsv([]), 'video_time_s,direction,note\n')
})

test('parseCsv reads what evaluate-flow writes and rejects what it rejects', () => {
  const py = '\uFEFFvideo_time_s,direction,note\r\n7.2,in,#1 car 0.87\r\n\r\n3,OUT\r\n'
  assert.deepEqual(parseCsv(py), [
    { t: 3, direction: 'out', note: '' },
    { t: 7.2, direction: 'in', note: '#1 car 0.87' },
  ])
  assert.deepEqual(parseCsv('video_time_s,direction\n1.5,in'), [
    { t: 1.5, direction: 'in', note: '' },
  ])
  const bad = [
    ['', /empty/],
    ['time,direction\n1,in\n', /header/],
    ['video_time_s,direction,note\n1,sideways,\n', /line 2: direction/],
    ['video_time_s,direction,note\nabc,in,\n', /number/],
    ['video_time_s,direction,note\n,in,\n', /number/],
    ['video_time_s,direction,note\n-1,in,\n', /number/],
    ['video_time_s,direction,note\n1,in,a,b\n', /columns/],
    ['video_time_s,direction\n1,in,van\n', /columns/],
    ['video_time_s,direction,note\n1\n', /columns/],
  ]
  for (const [text, msg] of bad) assert.throws(() => parseCsv(text, 'x.csv'), msg, text)
})

test('splitCsv handles quotes and line endings', () => {
  assert.deepEqual(splitCsv('a,"b,c"\r\n"d""e",\n'), [
    ['a', 'b,c'],
    ['d"e', ''],
  ])
  assert.deepEqual(splitCsv('x'), [['x']])
})

test('formatTime and csvName', () => {
  assert.equal(formatTime(0), '0:00.0')
  assert.equal(formatTime(125.37), '2:05.4')
  assert.equal(formatTime(59.96), '1:00.0')
  assert.equal(formatTime(3725.5), '1:02:05.5')
  assert.equal(csvName('rush-2026-10-12.mp4'), 'rush-2026-10-12.csv')
  assert.equal(csvName('night.clip.mkv'), 'night.clip.csv')
  assert.equal(csvName(''), 'clip.csv')
})
