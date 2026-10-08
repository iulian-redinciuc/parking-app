// i18next setup (frontend.md §1, §7): English is the fallback and, until open question #9 decides
// the UI languages, the only one. The language comes from `navigator.languages` (primary subtag:
// `ro-RO` → `ro`); a language without a file falls back to `en`. Resources are bundled, so `t()`
// works synchronously from the first render. To add a language: `locales/<lang>.json` with the
// same keys, then list it in `RESOURCES`.
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './locales/en.json'

export const FALLBACK_LANGUAGE = 'en'

const RESOURCES = {
  en: { translation: en },
} as const

export type Language = keyof typeof RESOURCES

export const LANGUAGES = Object.keys(RESOURCES) as Language[]

/** The first supported language in the browser's list (primary subtag only), else `en`. */
export function detectLanguage(
  preferred: readonly string[] = typeof navigator === 'undefined'
    ? []
    : navigator.languages?.length
      ? navigator.languages
      : [navigator.language],
): Language {
  for (const tag of preferred) {
    const primary = tag?.toLowerCase().split('-')[0]
    if (primary && (LANGUAGES as string[]).includes(primary)) return primary as Language
  }
  return FALLBACK_LANGUAGE
}

void i18n.use(initReactI18next).init({
  resources: RESOURCES,
  lng: detectLanguage(),
  fallbackLng: FALLBACK_LANGUAGE,
  supportedLngs: LANGUAGES,
  interpolation: { escapeValue: false }, // React escapes already
  initAsync: false,
  returnNull: false,
})

// Screen readers and the browser's hyphenation/translation follow the UI language.
const syncHtmlLang = (lng: string) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng
}
syncHtmlLang(i18n.language)
i18n.on('languageChanged', syncHtmlLang)

export const t = i18n.t.bind(i18n)
export default i18n
