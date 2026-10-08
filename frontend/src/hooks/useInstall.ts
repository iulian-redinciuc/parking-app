import { useSyncExternalStore } from 'react'
import { canInstall, subscribeInstall } from '../lib/install'

/** True while the browser offers installing the app (a saved `beforeinstallprompt`). */
export function useCanInstall(): boolean {
  return useSyncExternalStore(subscribeInstall, canInstall, () => false)
}
