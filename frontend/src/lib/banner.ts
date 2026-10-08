// Which edge state the Live screen is in (frontend.md §2.1): one banner, highest priority wins
// (offline > server_unreachable > unavailable > stale), plus whether the numbers are shown,
// greyed or replaced by the loading skeleton. The words are translated here, in the current language.
import type { LiveState, LotStatus } from '../api/types'
import { t } from '../i18n'

export type BannerKind = 'offline' | 'server_unreachable' | 'unavailable' | 'stale'

export interface Banner {
  kind: BannerKind
  /** i18n key of the title. */
  key: string
  title: string
  detail?: string
  tone: 'bad' | 'warn' | 'info'
}

export interface LiveView {
  banner: Banner | null
  /** Nothing yet and nothing wrong: show the skeleton. */
  loading: boolean
  /** The status to draw, or `null` to hide the numbers. */
  status: LotStatus | null
  /** Grey out every number (offline / unreachable: they're the last known ones). */
  dimAll: boolean
}

/** SSE and polling both failing for longer than this → "Can't reach the parking server". */
export const UNREACHABLE_AFTER_MS = 20_000

/** The age of the oldest stale zone's data, in whole minutes (at least 1), or `null` if unknown. */
export function staleMinutes(status: LotStatus, now: number): number | null {
  const times = status.zones
    .filter((z) => z.stale && z.updated_at !== null)
    .map((z) => Date.parse(z.updated_at as string))
    .filter((t) => !Number.isNaN(t))
  if (times.length === 0) return null
  return Math.max(1, Math.floor((now - Math.min(...times)) / 60_000))
}

function staleBanner(status: LotStatus, now: number): Banner {
  const minutes = staleMinutes(status, now)
  const zones = status.zones.filter((z) => z.stale).map((z) => z.name)
  return {
    kind: 'stale',
    key: minutes === null ? 'banner.stale_no_data' : 'banner.stale',
    title: minutes === null ? t('banner.stale_no_data') : t('banner.stale', { count: minutes }),
    detail: zones.length > 0 ? t('banner.affected', { zones: zones.join(', ') }) : undefined,
    tone: 'warn',
  }
}

/**
 * The view for a live state. `since` is when the app started, the reference for "failing for
 * > 20 s" before the first answer ever arrived.
 */
export function liveView(live: LiveState, now: number, since: number): LiveView {
  const { status, connection, error, lastMessageAt } = live
  const lastKnown = t(status ? 'banner.last_known' : 'banner.no_numbers')

  if (connection === 'offline') {
    return {
      banner: {
        kind: 'offline',
        key: 'banner.offline',
        title: t('banner.offline'),
        detail: lastKnown,
        tone: 'bad',
      },
      loading: false,
      status,
      dimAll: true,
    }
  }

  const failing =
    connection === 'error' ||
    (connection !== 'live' && error !== null && error.code !== 'unavailable')
  if (failing && now - (lastMessageAt ?? since) > UNREACHABLE_AFTER_MS) {
    return {
      banner: {
        kind: 'server_unreachable',
        key: 'banner.server_unreachable',
        title: t('banner.server_unreachable'),
        detail: lastKnown,
        tone: 'bad',
      },
      loading: false,
      status,
      dimAll: true,
    }
  }

  if (status === null && error?.code === 'unavailable') {
    return {
      banner: {
        kind: 'unavailable',
        key: 'banner.unavailable',
        title: t('banner.unavailable'),
        detail: t('banner.unavailable_detail'),
        tone: 'info',
      },
      loading: false,
      status: null,
      dimAll: false,
    }
  }

  if (status === null) return { banner: null, loading: true, status: null, dimAll: false }

  return {
    banner: status.total.stale ? staleBanner(status, now) : null,
    loading: false,
    status,
    dimAll: false,
  }
}
