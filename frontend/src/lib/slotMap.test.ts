import { describe, expect, it } from 'vitest'
import { parseSlotMap, slotState, slotsOffMap } from './slotMap'

const NS = 'xmlns="http://www.w3.org/2000/svg"'

describe('parseSlotMap', () => {
  it('keeps the shapes, their geometry and the slot ids', () => {
    const map = parseSlotMap(
      `<svg ${NS} viewBox="0 0 100 50">
         <rect id="G01" x="1" y="2" width="30" height="40" rx="3" transform="rotate(5 16 22)"/>
         <g transform="translate(40 0)"><polygon id="G02" points="0,0 30,0 30,40"/></g>
         <line x1="0" y1="45" x2="100" y2="45"/>
         <text x="50" y="49" text-anchor="middle" font-size="6">Entrance</text>
       </svg>`,
    )!
    expect(map.viewBox).toBe('0 0 100 50')
    expect(map.ids).toEqual(['G01', 'G02'])
    const [rect, group, line, text] = map.nodes
    expect(rect).toMatchObject({
      tag: 'rect',
      slotId: 'G01',
      attrs: { x: '1', y: '2', width: '30', height: '40', rx: '3', transform: 'rotate(5 16 22)' },
    })
    expect(group.attrs).toEqual({ transform: 'translate(40 0)' })
    expect(group.children[0]).toMatchObject({ tag: 'polygon', slotId: 'G02' })
    expect(line.slotId).toBeUndefined()
    expect(text).toMatchObject({
      text: 'Entrance',
      attrs: { textAnchor: 'middle', fontSize: '6' },
    })
  })

  it('drops everything that is not plain geometry', () => {
    const map = parseSlotMap(
      `<svg ${NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10" onload="alert(1)">
         <script>alert(1)</script>
         <style>rect { fill: url(https://example.com/x) }</style>
         <image href="https://example.com/x.png" width="10" height="10"/>
         <a xlink:href="javascript:alert(1)"><rect id="G09" width="1" height="1"/></a>
         <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>
         <rect id="G01" width="5" height="5" onclick="alert(1)" style="fill:red" fill="url(#x)"
               x="javascript:alert(1)" class="x"/>
         <line id="lane" x1="0" y1="0" x2="1" y2="1"/>
       </svg>`,
    )!
    expect(map.nodes.map((n) => n.tag)).toEqual(['rect', 'line'])
    expect(map.nodes[0].attrs).toEqual({ width: '5', height: '5' })
    // an id makes a slot only on a shape
    expect(map.ids).toEqual(['G01'])
  })

  it.each([
    ['not XML', '<svg'],
    ['another root', '<html xmlns="http://www.w3.org/1999/xhtml"/>'],
    ['no namespace', '<svg viewBox="0 0 1 1"/>'],
    ['no viewBox', `<svg ${NS}/>`],
    ['a bad viewBox', `<svg ${NS} viewBox="0 0 10"/>`],
  ])('answers null for %s', (_, svg) => {
    expect(parseSlotMap(svg)).toBeNull()
  })
})

describe('slot states', () => {
  const slots = { G01: true, G02: false }

  it('reads free, taken and unknown', () => {
    expect(slotState(slots, 'G01')).toBe('taken')
    expect(slotState(slots, 'G02')).toBe('free')
    expect(slotState(slots, 'G03')).toBe('unknown')
    expect(slotState(slots, 'toString')).toBe('unknown')
  })

  it('lists the slots without a shape', () => {
    expect(slotsOffMap(slots, ['G02', 'G03'])).toEqual(['G01'])
    expect(slotsOffMap(slots, ['G01', 'G02'])).toEqual([])
  })
})
