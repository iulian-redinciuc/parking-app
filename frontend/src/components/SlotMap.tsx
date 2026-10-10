import { createElement, useEffect, useId, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { getSlotMap } from '../api/client'
import type { ZoneStatus } from '../api/types'
import { parseSlotMap, slotState, slotsOffMap } from '../lib/slotMap'
import type { MapNode, SlotMapData, SlotState } from '../lib/slotMap'

// Colours come from `currentColor`, so a dimmed card (old numbers) greys the map too; free and
// taken still differ by how solid they are, not only by colour.
const SLOT_CLASS: Record<SlotState, string> = {
  free: 'text-ok',
  taken: 'text-muted',
  unknown: 'text-muted',
}
const OPEN_KEY = 'parking.map.open'

function wasOpen(zoneId: string): boolean {
  try {
    return localStorage.getItem(`${OPEN_KEY}.${zoneId}`) === '1'
  } catch {
    return false
  }
}

function rememberOpen(zoneId: string, open: boolean): void {
  try {
    localStorage.setItem(`${OPEN_KEY}.${zoneId}`, open ? '1' : '0')
  } catch {
    // private mode: the map just starts closed next time
  }
}

/** The zone's map file, parsed; `null` while loading, without a map, or when it can't be read. */
function useSlotMap(zoneId: string, enabled: boolean): SlotMapData | null {
  const [loaded, setLoaded] = useState<{ zoneId: string; map: SlotMapData | null } | null>(null)
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    getSlotMap(zoneId, { signal: controller.signal }).then(
      (svg) => setLoaded({ zoneId, map: svg === null ? null : parseSlotMap(svg) }),
      () => {}, // no map is shown; the numbers don't depend on it
    )
    return () => controller.abort()
  }, [zoneId, enabled])
  return enabled && loaded?.zoneId === zoneId ? loaded.map : null
}

// Which spaces are free (P9.1, frontend.md §2.1): a *Show map* switch on a `slots` zone's card,
// the schematic coloured from `zone.slots`, and the id of the space that was tapped.
export default function SlotMap({ zone }: { zone: ZoneStatus }) {
  const { t } = useTranslation()
  const slots = zone.slots
  const map = useSlotMap(zone.id, slots !== null)
  const [open, setOpen] = useState(() => wasOpen(zone.id))
  const [selected, setSelected] = useState<string | null>(null)
  const panelId = useId()
  if (!slots || !map || map.ids.length === 0) return null

  const stateWord = (id: string) => t(`map.${slotState(slots, id)}`)
  const toggle = () => {
    rememberOpen(zone.id, !open)
    setOpen(!open)
  }
  const offMap = slotsOffMap(slots, map.ids)

  function draw(node: MapNode, key: number): ReactNode {
    if (node.slotId === undefined) {
      // decoration (lanes, labels): outlines and words in the muted colour
      const isText = node.tag === 'text'
      return createElement(
        node.tag,
        {
          key,
          ...node.attrs,
          ...(node.tag !== 'g' && {
            className: 'text-muted',
            fill: isText ? 'currentColor' : 'none',
            stroke: isText ? undefined : 'currentColor',
            strokeWidth: isText ? undefined : 2,
            vectorEffect: 'non-scaling-stroke',
          }),
        },
        isText ? node.text : node.children.map(draw),
      )
    }
    const id = node.slotId
    const state = slotState(slots!, id)
    const isSelected = selected === id
    const select = () => setSelected(isSelected ? null : id)
    return createElement(node.tag, {
      key,
      ...node.attrs,
      role: 'button',
      tabIndex: 0,
      'aria-label': t('map.slot', { id, state: stateWord(id) }),
      'aria-pressed': isSelected,
      'data-slot': id,
      'data-state': state,
      className: `cursor-pointer outline-none focus-visible:[stroke-width:3] focus-visible:[stroke:var(--accent)] ${SLOT_CLASS[state]}`,
      fill: state === 'unknown' ? 'transparent' : 'currentColor',
      fillOpacity: state === 'taken' ? 0.3 : 1,
      stroke: isSelected ? 'var(--text)' : state === 'unknown' ? 'currentColor' : 'none',
      strokeWidth: isSelected ? 3 : 1.5,
      strokeDasharray: state === 'unknown' && !isSelected ? '4 3' : undefined,
      vectorEffect: 'non-scaling-stroke',
      onClick: select,
      onKeyDown: (event: KeyboardEvent) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        select()
      },
    })
  }

  return (
    <div className="flex flex-col gap-2" data-testid={`map-${zone.id}`}>
      <button
        type="button"
        className="min-h-11 self-start text-sm font-semibold text-accent"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
      >
        {t(open ? 'map.hide' : 'map.show')}
      </button>
      {open && (
        <div id={panelId} className="flex flex-col gap-2">
          <svg
            viewBox={map.viewBox}
            role="group"
            aria-label={t('map.label', { zone: zone.name })}
            className="max-h-96 w-full"
          >
            {map.nodes.map(draw)}
          </svg>
          <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted">
            <span className="flex items-center gap-1.5">
              <span aria-hidden="true" className="size-3 rounded-sm bg-current text-ok" />
              {t('map.free')}
            </span>
            <span className="flex items-center gap-1.5">
              <span aria-hidden="true" className="size-3 rounded-sm bg-current opacity-30" />
              {t('map.taken')}
            </span>
          </p>
          <p className="text-sm" role="status">
            {selected ? t('map.slot', { id: selected, state: stateWord(selected) }) : t('map.hint')}
          </p>
          {offMap.length > 0 && (
            <p className="text-sm text-muted">{t('map.off_map', { count: offMap.length })}</p>
          )}
        </div>
      )}
    </div>
  )
}
