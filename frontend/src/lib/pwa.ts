// Pure helpers for the service worker and the install UX (frontend.md §5).

/** API paths: live data must never come from a cache, so the service worker never answers these. */
export const API_PATH = /(^|\/)api\//

/** Where a notification tap goes: its `data.url` if it's ours (same origin), else the app's start. */
export function notificationUrl(data: unknown, scope: string): string {
  const raw =
    typeof data === 'object' && data !== null && 'url' in data && typeof data.url === 'string'
      ? data.url
      : ''
  if (!raw) return scope
  try {
    const url = new URL(raw, scope)
    return url.origin === new URL(scope).origin ? url.href : scope
  } catch {
    return scope
  }
}

/** iPhone/iPad Safari (iPadOS reports itself as a Mac with touch). Other iOS browsers can't install. */
export function isIosSafari(nav: Pick<Navigator, 'userAgent' | 'maxTouchPoints'>): boolean {
  const ua = nav.userAgent
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && nav.maxTouchPoints > 1)
  return ios && /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|GSA\//.test(ua)
}

/** Opened from the home screen (or as an installed desktop app). */
export function isStandalone(win: Window = window): boolean {
  return (
    win.matchMedia?.('(display-mode: standalone)').matches === true ||
    (win.navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}
