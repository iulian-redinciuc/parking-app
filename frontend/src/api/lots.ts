// Which lot the app shows (docs/design/multi-lot.md §6). A server counts one lot or several;
// the choice is kept on this device. Nothing chosen = the server's default lot, reached over
// the paths without a lot (`/api/status`), which every server version has.

const KEY = 'parking.lot'
const LOT_ID = /^[a-z0-9-]{1,64}$/

/** The lot picked on this device, or `null` for the server's default lot. */
export function selectedLot(): string | null {
  try {
    const id = localStorage.getItem(KEY)
    return id !== null && LOT_ID.test(id) ? id : null
  } catch {
    return null // private mode
  }
}

export function selectLot(id: string | null): void {
  try {
    if (id === null) localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, id)
  } catch {
    // private mode: the default lot it is
  }
}

/**
 * The path of a lot resource: `lotPath('/status')` is `/api/lots/<id>/status` for the picked lot
 * and `/api/status` without one; `lotPath()` is the lot info itself (`/api/lots/<id>`, `/api/lot`).
 */
export function lotPath(resource = '', lot = selectedLot()): string {
  if (lot === null) return resource ? `/api${resource}` : '/api/lot'
  return `/api/lots/${encodeURIComponent(lot)}${resource}`
}
