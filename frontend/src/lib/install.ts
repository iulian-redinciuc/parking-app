// "Install app" on Android/desktop (frontend.md §5): the browser's `beforeinstallprompt` is kept
// so a button can show the install dialog later. Captured from app start (the event can fire
// before React mounts), exposed as a tiny store for useSyncExternalStore.

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

let deferred: BeforeInstallPromptEvent | null = null
const listeners = new Set<() => void>()
let started = false

function set(next: BeforeInstallPromptEvent | null) {
  deferred = next
  listeners.forEach((l) => l())
}

export function startInstallCapture(win: Window = window) {
  if (started) return
  started = true
  win.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault() // no mini-infobar; our button offers it instead
    set(e as BeforeInstallPromptEvent)
  })
  win.addEventListener('appinstalled', () => set(null))
}

export function subscribeInstall(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export const canInstall = () => deferred !== null

/** Shows the browser's install dialog; the saved event can be used once either way. */
export async function promptInstall(): Promise<boolean> {
  const event = deferred
  if (!event) return false
  set(null)
  await event.prompt()
  return (await event.userChoice).outcome === 'accepted'
}
