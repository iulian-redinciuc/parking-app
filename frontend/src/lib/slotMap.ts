// The slot map (docs/design/slot-map.md): the SVG from `GET /api/maps/<zone>` is never put into
// the page as markup. It is parsed, and only plain shapes with geometry attributes are kept; the
// component draws those itself, so scripts, styles, links and images in the file do nothing.

/** Elements that are a slot when they carry a slot id (backend: parking/slot_map.py `SHAPES`). */
export const SLOT_SHAPES = ['rect', 'polygon', 'path', 'circle', 'ellipse']
const TAGS = new Set([...SLOT_SHAPES, 'g', 'line', 'polyline', 'text'])
// SVG attribute → React prop
const ATTRS: Record<string, string> = {
  x: 'x',
  y: 'y',
  width: 'width',
  height: 'height',
  rx: 'rx',
  ry: 'ry',
  cx: 'cx',
  cy: 'cy',
  r: 'r',
  x1: 'x1',
  y1: 'y1',
  x2: 'x2',
  y2: 'y2',
  points: 'points',
  d: 'd',
  transform: 'transform',
  'font-size': 'fontSize',
  'text-anchor': 'textAnchor',
}
// numbers, path letters, transform names: nothing that could be a URL or a script
const SAFE_VALUE = /^[\w\s.,()+\-%]*$/
const VIEW_BOX = /^\s*-?[\d.]+([\s,]+-?[\d.]+){3}\s*$/
const SVG_NS = 'http://www.w3.org/2000/svg'

export interface MapNode {
  tag: string
  /** React props, geometry only. */
  attrs: Record<string, string>
  /** Set on slot shapes. */
  slotId?: string
  /** The words of a `<text>`. */
  text?: string
  children: MapNode[]
}

export interface SlotMapData {
  viewBox: string
  nodes: MapNode[]
  /** Slot ids on the map, in document order. */
  ids: string[]
}

function readNode(el: Element, ids: string[]): MapNode | null {
  const tag = el.localName
  if (el.namespaceURI !== SVG_NS || !TAGS.has(tag)) return null
  const attrs: Record<string, string> = {}
  for (const { name, value } of Array.from(el.attributes)) {
    if (name in ATTRS && SAFE_VALUE.test(value)) attrs[ATTRS[name]] = value
  }
  const node: MapNode = { tag, attrs, children: [] }
  const id = el.getAttribute('id')
  if (id && SLOT_SHAPES.includes(tag)) {
    node.slotId = id
    ids.push(id)
  }
  if (tag === 'text') node.text = el.textContent ?? ''
  else if (tag === 'g') {
    for (const child of Array.from(el.children)) {
      const kept = readNode(child, ids)
      if (kept) node.children.push(kept)
    }
  }
  return node
}

/** The drawable parts of a map file, or `null` when it isn't an SVG with a `viewBox`. */
export function parseSlotMap(svg: string): SlotMapData | null {
  const root = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement
  if (root.localName !== 'svg' || root.namespaceURI !== SVG_NS) return null
  const viewBox = root.getAttribute('viewBox') ?? ''
  if (!VIEW_BOX.test(viewBox)) return null
  const ids: string[] = []
  const nodes: MapNode[] = []
  for (const child of Array.from(root.children)) {
    const kept = readNode(child, ids)
    if (kept) nodes.push(kept)
  }
  return { viewBox: viewBox.trim(), nodes, ids }
}

export type SlotState = 'free' | 'taken' | 'unknown'

/** A slot's state from `ZoneStatus.slots`; `unknown` = on the map but not in the status. */
export function slotState(slots: Record<string, boolean>, id: string): SlotState {
  if (!Object.hasOwn(slots, id)) return 'unknown'
  return slots[id] ? 'taken' : 'free'
}

/** Slots in the status that the map has no shape for. */
export function slotsOffMap(slots: Record<string, boolean>, ids: string[]): string[] {
  const onMap = new Set(ids)
  return Object.keys(slots).filter((id) => !onMap.has(id))
}
