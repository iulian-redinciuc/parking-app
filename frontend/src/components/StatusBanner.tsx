import type { Banner } from '../lib/banner'

// Full class names, so Tailwind finds them.
const TONE: Record<Banner['tone'], { box: string; icon: string }> = {
  bad: { box: 'border-bad', icon: 'text-bad' },
  warn: { box: 'border-warn', icon: 'text-warn' },
  info: { box: 'border-accent', icon: 'text-accent' },
}

function Icon({ kind }: { kind: Banner['kind'] }) {
  const common = {
    'aria-hidden': true,
    viewBox: '0 0 24 24',
    className: 'size-6 shrink-0',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  }
  switch (kind) {
    case 'offline': // wifi, crossed out
      return (
        <svg {...common}>
          <path d="M2 8.8a15 15 0 0 1 20 0M5 12.6a10 10 0 0 1 14 0M8.5 16.4a5 5 0 0 1 7 0M12 20h.01M3 3l18 18" />
        </svg>
      )
    case 'server_unreachable': // cloud, crossed out
      return (
        <svg {...common}>
          <path d="M7 18h10a4 4 0 0 0 .8-7.9A6 6 0 0 0 6.2 9.5 4.3 4.3 0 0 0 7 18ZM3 3l18 18" />
        </svg>
      )
    case 'unavailable': // hourglass
      return (
        <svg {...common}>
          <path d="M6 2h12M6 22h12M7 2v4l5 6-5 6v4M17 2v4l-5 6 5 6v4" />
        </svg>
      )
    case 'stale': // clock
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </svg>
      )
  }
}

// The one banner at the top of the Live screen (frontend.md §2.1): the highest-priority edge
// state, always as words with an icon, never only a colour.
export default function StatusBanner({ banner }: { banner: Banner }) {
  const tone = TONE[banner.tone]
  return (
    <div
      role="status"
      data-testid="status-banner"
      data-kind={banner.kind}
      className={`mt-3 flex items-start gap-3 rounded-xl border-l-4 bg-surface p-4 ${tone.box}`}
    >
      <span className={tone.icon}>
        <Icon kind={banner.kind} />
      </span>
      <div className="min-w-0">
        <p className="font-semibold">{banner.title}</p>
        {banner.detail && <p className="text-sm text-muted">{banner.detail}</p>}
      </div>
    </div>
  )
}

// Grey placeholders the shape of the Live screen while nothing has arrived yet.
export function LiveSkeleton() {
  const block = 'animate-pulse rounded-lg bg-muted/20'
  return (
    <div role="status" aria-busy="true" data-testid="live-skeleton" className="flex flex-col gap-3">
      <span className="sr-only">Loading the parking status…</span>
      <div aria-hidden="true" className="flex flex-col items-center gap-3 py-8">
        <div className={`h-20 w-28 ${block}`} />
        <div className={`h-5 w-24 ${block}`} />
        <div className={`mt-4 h-2.5 w-full max-w-xs ${block}`} />
      </div>
      {[0, 1].map((i) => (
        <div key={i} aria-hidden="true" className="flex flex-col gap-3 rounded-xl bg-surface p-4">
          <div className="flex justify-between">
            <div className={`h-6 w-28 ${block}`} />
            <div className={`h-6 w-20 ${block}`} />
          </div>
          <div className={`h-2.5 w-full ${block}`} />
        </div>
      ))}
    </div>
  )
}
