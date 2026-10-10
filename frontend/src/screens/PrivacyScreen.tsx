import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getLot } from '../api/client'
import type { LotInfo } from '../api/types'

// The Privacy screen (frontend.md §2.5, security-privacy.md §4.1): static text, one list per
// topic. Each topic is `privacy.<id>` + `privacy.<id>_1…n` in the locale files; the numbers in
// the text (90 days, 30 days, 24 hours) are the retention periods of data-model.md §4.
const SECTIONS = [
  ['cameras', 6],
  ['stored', 3],
  ['location', 2],
  ['notifications', 4],
  ['device', 2],
  ['logs', 1],
] as const

type Operator = NonNullable<LotInfo['privacy']>

export default function PrivacyScreen() {
  const { t } = useTranslation()
  // who runs the cameras comes from the server's settings; without it the signs at the lot say
  const [who, setWho] = useState<Operator | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    getLot({ signal: controller.signal }).then(
      (lot) => setWho(lot.privacy ?? null),
      () => {},
    )
    return () => controller.abort()
  }, [])

  const contact = who?.contact
  return (
    <article className="flex flex-col gap-5 py-4">
      <h1 className="text-2xl font-bold">{t('screen.privacy')}</h1>
      <p>{t('privacy.intro')}</p>
      {SECTIONS.map(([id, count]) => (
        <section key={id} aria-labelledby={`privacy-${id}`} className="flex flex-col gap-2">
          <h2 id={`privacy-${id}`} className="text-lg font-semibold">
            {t(`privacy.${id}`)}
          </h2>
          <ul className="flex list-disc flex-col gap-1.5 pl-5">
            {Array.from({ length: count }, (_, i) => (
              <li key={i}>{t(`privacy.${id}_${i + 1}`)}</li>
            ))}
          </ul>
        </section>
      ))}
      <section aria-labelledby="privacy-who" className="flex flex-col gap-2">
        <h2 id="privacy-who" className="text-lg font-semibold">
          {t('privacy.who')}
        </h2>
        <p>
          {who?.operator
            ? t('privacy.who_operator', { operator: who.operator })
            : t('privacy.who_signs')}
        </p>
        {contact && (
          <p>
            {t('privacy.who_contact')}{' '}
            {/^[^\s@]+@[^\s@]+$/.test(contact) ? (
              <a href={`mailto:${contact}`} className="text-accent underline">
                {contact}
              </a>
            ) : (
              contact
            )}
          </p>
        )}
        <p>{t('privacy.who_rights')}</p>
      </section>
    </article>
  )
}
