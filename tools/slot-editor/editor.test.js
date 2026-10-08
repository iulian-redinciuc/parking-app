// Unit tests for the pure parts of editor.js. Run with: node --test tools/slot-editor/
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import {
  centroid,
  cycleLabel,
  duplicateSlot,
  formatJson,
  isSelfIntersecting,
  nextSlotId,
  parseLabelsFile,
  parseLineFile,
  parseSlotFile,
  pointInPolygon,
  setLabel,
  sideOffset,
  toLabelsFile,
  toLineFile,
  toSlotFile,
  validateSlots,
} from './editor.js'

const square = [
  [0, 0],
  [10, 0],
  [10, 10],
  [0, 10],
]
// The two example slots from docs/design/config.md §2
const G01 = { id: 'G01', zone: 'ground', polygon: [[412, 980], [598, 975], [640, 1180], [430, 1190]], type: 'standard' } // prettier-ignore
const G02 = { id: 'G02', zone: 'ground', polygon: [[598, 975], [781, 970], [842, 1172], [640, 1180]], type: 'accessible' } // prettier-ignore

test('centroid and point in polygon', () => {
  assert.deepEqual(centroid(square), [5, 5])
  assert.ok(pointInPolygon([5, 5], square))
  assert.ok(!pointInPolygon([15, 5], square))
})

test('self-intersection', () => {
  assert.ok(!isSelfIntersecting(square))
  assert.ok(!isSelfIntersecting(G01.polygon))
  const bowtie = [
    [0, 0],
    [10, 10],
    [10, 0],
    [0, 10],
  ]
  assert.ok(isSelfIntersecting(bowtie))
})

test('next slot id uses the zone letter and the highest number', () => {
  assert.equal(nextSlotId([], 'ground'), 'G01')
  assert.equal(nextSlotId([G01, { ...G02, id: 'G07' }], 'ground'), 'G08')
  assert.equal(nextSlotId([G01], 'underground'), 'U01')
})

test('duplicate right shifts by the width and shares the edge', () => {
  assert.deepEqual(sideOffset(square, 'right'), [10, 0])
  assert.deepEqual(sideOffset(square, 'down'), [0, 10])
  const copy = duplicateSlot(G01, [G01], 'right')
  assert.equal(copy.id, 'G02')
  assert.equal(copy.type, 'standard')
  // the copy's left edge sits roughly on the original's right edge
  const [dx, dy] = sideOffset(G01.polygon, 'right')
  assert.deepEqual(copy.polygon[0], [412 + dx, 980 + dy])
  assert.ok(Math.abs(copy.polygon[0][0] - 598) < 40)
  const down = duplicateSlot({ ...G01, polygon: square }, [G01], 'down')
  assert.deepEqual(
    down.polygon,
    square.map(([x, y]) => [x, y + 10]),
  )
})

test('validation mirrors load_slots()', () => {
  assert.deepEqual(validateSlots([G01, G02]), [])
  const errors = validateSlots([G01, { ...G01 }, { id: 'X', zone: 'g', polygon: [[0, 0], [1, 1]], type: 'big' }]) // prettier-ignore
  assert.equal(errors.length, 3)
})

test('slot file round-trips to identical text', () => {
  const file = {
    version: 1,
    camera_id: 'cam-ground',
    image_size: [2560, 1440],
    reference_image: 'data/reference/cam-ground.jpg',
    slots: [G01, G02],
    count_zones: [{ zone: 'ground', polygon: [[0, 600], [2560, 600], [2560, 1440], [0, 1440]] }], // prettier-ignore
  }
  const text = formatJson(file)
  const again = formatJson(toSlotFile(parseSlotFile(JSON.parse(text))))
  assert.equal(again, text)
  assert.deepEqual(JSON.parse(text), file)
  assert.match(text, /\n {4}\{ "id": "G01", "zone": "ground", "polygon": \[\[412, 980\], /)
})

test('slot file rounds points and defaults type', () => {
  const out = toSlotFile({
    cameraId: 'c',
    imageSize: [100, 50],
    slots: [{ id: 'G01', zone: 'ground', polygon: [[1.4, 2.6], [10, 0], [10, 10]] }], // prettier-ignore
  })
  assert.deepEqual(out.slots[0], { id: 'G01', zone: 'ground', polygon: [[1, 3], [10, 0], [10, 10]], type: 'standard' }) // prettier-ignore
  assert.deepEqual(out.count_zones, [])
  assert.equal('reference_image' in out, false)
  assert.throws(() => parseSlotFile({ version: 2 }))
})

test('line file round-trips', () => {
  const file = {
    version: 1,
    camera_id: 'cam-ramp',
    image_size: [640, 360],
    roi: [[60, 120], [600, 120], [600, 360], [60, 360]], // prettier-ignore
    line_a: [[100, 220], [560, 220]], // prettier-ignore
    line_b: [[100, 270], [560, 270]], // prettier-ignore
    in_direction: 'a_to_b',
  }
  const text = formatJson(file)
  assert.equal(formatJson(toLineFile(parseLineFile(JSON.parse(text)))), text)
  const noRoi = toLineFile({ ...parseLineFile(file), roi: null, inDirection: 'b_to_a' })
  assert.equal('roi' in noRoi, false)
  assert.equal(noRoi.in_direction, 'b_to_a')
  assert.throws(() => toLineFile({ cameraId: 'c', imageSize: [1, 1], lineA: null }))
})

test('labels: cycle, set and round-trip', () => {
  assert.equal(cycleLabel('free'), 'taken')
  assert.equal(cycleLabel('taken'), 'unsure')
  assert.equal(cycleLabel('unsure'), 'free')
  const order = ['G01', 'G02', 'G04', 'G09']
  let e = setLabel(undefined, 'G04', 'taken', order)
  e = setLabel(e, 'G01', 'taken', order)
  e = setLabel(e, 'G09', 'unsure', order)
  assert.deepEqual(e, { conditions: [], taken: ['G01', 'G04'], unsure: ['G09'] })
  e = setLabel(e, 'G04', 'free', order)
  assert.deepEqual(e.taken, ['G01'])

  const file = {
    version: 1,
    camera_id: 'cam-ground',
    images: {
      'ground-01.jpg': { conditions: ['day', 'dry'], taken: ['G01', 'G04'], unsure: ['G09'] },
      'ground-02.jpg': { conditions: ['night'], taken: ['G01'], unsure: [] },
    },
  }
  const text = formatJson(file)
  assert.equal(formatJson(toLabelsFile(parseLabelsFile(JSON.parse(text)))), text)
  assert.match(text, /\n {4}"ground-02\.jpg": \{ "conditions": \["night"\], /)
})

test('the committed cam-ground slot file is in the exported format', () => {
  const url = new URL('../../config/slots/cam-ground.json', import.meta.url)
  const text = readFileSync(url, 'utf8')
  const parsed = parseSlotFile(JSON.parse(text))
  assert.equal(formatJson(toSlotFile(parsed)), text)
  assert.deepEqual(validateSlots(parsed.slots), [])
})
