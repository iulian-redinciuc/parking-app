import { describe, expect, it } from 'vitest'
import { API_PATH, isIosSafari, isStandalone, notificationUrl } from './pwa'

const SCOPE = 'https://example.org/parking-app/'

describe('API_PATH', () => {
  it('matches API paths at any base', () => {
    for (const p of ['/api/status', '/api/stream', '/parking-app/api/lot', '/api/admin/zones'])
      expect(API_PATH.test(p), p).toBe(true)
  })
  it('leaves app paths alone', () => {
    for (const p of ['/parking-app/', '/parking-app/index.html', '/parking-app/assets/rapid.js'])
      expect(API_PATH.test(p), p).toBe(false)
  })
})

describe('notificationUrl', () => {
  it('uses a same-origin url, relative or absolute', () => {
    expect(notificationUrl({ url: '#/alerts' }, SCOPE)).toBe(`${SCOPE}#/alerts`)
    expect(notificationUrl({ url: `${SCOPE}#/` }, SCOPE)).toBe(`${SCOPE}#/`)
  })
  it('falls back to the scope for missing, odd or foreign urls', () => {
    for (const data of [
      undefined,
      null,
      {},
      { url: 3 },
      { url: '' },
      { url: 'https://evil.test/' },
    ])
      expect(notificationUrl(data, SCOPE)).toBe(SCOPE)
  })
})

const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'

describe('isIosSafari', () => {
  const nav = (userAgent: string, maxTouchPoints = 5) => ({ userAgent, maxTouchPoints })
  it('spots iPhone Safari and iPadOS (desktop UA with touch)', () => {
    expect(isIosSafari(nav(IPHONE_SAFARI))).toBe(true)
    const ipad =
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
    expect(isIosSafari(nav(ipad))).toBe(true)
    expect(isIosSafari(nav(ipad, 0))).toBe(false) // a real Mac
  })
  it('ignores other iOS browsers and Android', () => {
    expect(isIosSafari(nav(IPHONE_SAFARI.replace('Version/18.0', 'CriOS/129.0')))).toBe(false)
    expect(isIosSafari(nav(IPHONE_SAFARI.replace('Version/18.0', 'FxiOS/131.0')))).toBe(false)
    const android =
      'Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36'
    expect(isIosSafari(nav(android))).toBe(false)
  })
})

describe('isStandalone', () => {
  const win = (matches: boolean, standalone?: boolean) =>
    ({ matchMedia: () => ({ matches }), navigator: { standalone } }) as unknown as Window
  it('is true in display-mode standalone or iOS home-screen mode', () => {
    expect(isStandalone(win(true))).toBe(true)
    expect(isStandalone(win(false, true))).toBe(true)
    expect(isStandalone(win(false))).toBe(false)
  })
})
