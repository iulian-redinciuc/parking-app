import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

// Outline icons on a 24×24 grid; the label below always says what the tab is.
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="size-6"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  )
}

const TABS = [
  {
    to: '/',
    label: 'Live',
    icon: (
      <Icon>
        <rect x="4" y="3" width="16" height="18" rx="2" />
        <path d="M10 17V7h3a3 3 0 0 1 0 6h-3" />
      </Icon>
    ),
  },
  {
    to: '/alerts',
    label: 'Alerts',
    icon: (
      <Icon>
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
        <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
      </Icon>
    ),
  },
  {
    to: '/stats',
    label: 'Stats',
    icon: (
      <Icon>
        <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
      </Icon>
    ),
  },
]

export default function BottomNav() {
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-10 border-t border-surface bg-bg pr-[env(safe-area-inset-right)] pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)]"
    >
      <ul className="mx-auto grid h-16 max-w-xl grid-cols-3">
        {TABS.map(({ to, label, icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              end
              className={({ isActive }) =>
                `flex h-full min-h-11 flex-col items-center justify-center gap-0.5 text-xs font-medium focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
                  isActive ? 'text-accent' : 'text-muted'
                }`
              }
            >
              {icon}
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
