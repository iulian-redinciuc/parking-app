import { describe, expect, it } from 'vitest'
import type { Connection } from '../api/types'
import { LEVELS, TRENDS } from '../lib/status'
import i18n, { FALLBACK_LANGUAGE, LANGUAGES, detectLanguage, t } from '.'
import en from './locales/en.json'

// Every key in a locale file, as "a.b" paths.
function keys(tree: object, prefix = ''): string[] {
  return Object.entries(tree).flatMap(([k, v]) =>
    typeof v === 'object' ? keys(v, `${prefix}${k}.`) : [`${prefix}${k}`],
  )
}

describe('i18n', () => {
  it('starts in English with en as the fallback, synchronously', () => {
    expect(i18n.isInitialized).toBe(true)
    expect(i18n.language).toBe('en')
    expect(FALLBACK_LANGUAGE).toBe('en')
    expect(LANGUAGES).toContain('en')
    expect(document.documentElement.lang).toBe('en')
  })

  it('detects the first supported language by primary subtag, else en', () => {
    expect(detectLanguage(['en-GB', 'ro'])).toBe('en')
    expect(detectLanguage(['EN-us'])).toBe('en')
    expect(detectLanguage(['xx-YY', 'zz'])).toBe('en')
    expect(detectLanguage([])).toBe('en')
  })

  it('pluralises with i18next plural keys', () => {
    expect(t('count.free', { count: 0 })).toBe('free spaces')
    expect(t('count.free', { count: 1 })).toBe('free space')
    expect(t('count.free', { count: 23 })).toBe('free spaces')
    expect(t('banner.stale', { count: 1 })).toBe('Camera data is 1 min old')
    expect(t('banner.stale', { count: 7 })).toBe('Camera data is 7 min old')
  })

  it('has a word for every level, trend and connection state', () => {
    const connections: Connection[] = ['connecting', 'live', 'polling', 'offline', 'error']
    const needed = [
      ...Object.values(LEVELS).map((l) => l.key),
      ...Object.values(TRENDS).map((tr) => tr.key),
      ...connections.map((c) => `connection.${c}`),
    ]
    for (const key of needed) expect(i18n.exists(key), key).toBe(true)
  })

  it('has no empty strings', () => {
    const all = keys(en)
    expect(all.length).toBeGreaterThan(30)
    for (const key of all) expect(i18n.t(key as never), key).not.toBe('')
  })
})
