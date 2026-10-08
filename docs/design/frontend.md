# Frontend (mobile-first PWA)

## 1. Stack

| Concern | Choice | Notes |
|---------|--------|-------|
| Build | **Vite** | `base` from `VITE_BASE` (`/parking-app/` for the GitHub Pages preview, usually `/` in production) |
| UI | **React 19 + TypeScript** (strict) | |
| Routing | **react-router `HashRouter`** | Works on any static host without server rewrite rules (GitHub Pages has none), so `#/stats` never 404s on reload |
| Styling | **Tailwind CSS v4** + CSS custom properties for tokens | Light and dark |
| i18n | **i18next + react-i18next** | `en` first; files in `src/i18n/locales/*.json` |
| PWA | **vite-plugin-pwa**, `strategies: 'injectManifest'` | A custom `src/sw.ts`, because we need push handlers |
| Charts (Phase 7) | **Recharts** | Lazy-loaded with the Stats screen |
| Tests | **Vitest + Testing Library**, **Playwright** | Playwright projects: `iPhone 13`, `Pixel 7` |
| Lint/format | ESLint (typescript-eslint, react-hooks), Prettier | |

No global state library. Live status is a small external store read with `useSyncExternalStore`.

## 2. Screens

Bottom navigation with three tabs: **Live · Alerts · Stats**. Admin is reached via `#/admin`, with no tab.

### 2.1 Live (`#/`)
```
┌──────────────────────────────┐
│ Parking            ● Live    │  header: name, connection dot
├──────────────────────────────┤
│                              │
│            23                │  BigCount (≈ prefix if confidence < 0.8)
│        free spaces           │
│     ▓▓▓▓▓▓▓▓▓▓▓▓░░░  Plenty  │  level word + colour + bar
│                              │
├──────────────────────────────┤
│ Ground            12 / 40 ↘  │  ZoneCard: free/capacity, trend arrow
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░ Filling up│
├──────────────────────────────┤
│ Underground     ≈ 11 / 60 →  │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░ Filling up│
│ ⓘ Estimated from entry/exit  │  shown when confidence < 0.8
├──────────────────────────────┤
│ Updated 3 s ago              │  UpdatedAgo
└──────────────────────────────┘
 [ Live ]   [ Alerts ]   [ Stats ]
```

**States** (one `StatusBanner` at the top, highest priority wins):

| State | Trigger | Banner | Numbers |
|-------|---------|--------|---------|
| `loading` | first load, nothing yet | skeleton | hidden |
| `offline` | `navigator.onLine === false` | "You're offline" | last known, greyed |
| `server_unreachable` | SSE and polling both failing for > 20 s | "Can't reach the parking server" | last known, greyed |
| `stale` | `total.stale` | "Camera data is X min old" | shown, greyed for stale zones |
| `estimated` | any zone confidence < 0.8 | inline note on that card | shown with ≈ |
| `unavailable` | API 503 | "Waiting for the first camera reading" | hidden |

### 2.2 Alerts (`#/alerts`): Phase 6
- **Enable notifications** button: never asks for permission until tapped.
- On iPhone and not installed: an **InstallHint** card ("Tap Share → Add to Home Screen, then open from the icon") instead of the button.
- Toggles: *Tell me when I'm near* (radius slider 200–2000 m), *Warn when almost full*, zone filter.
- **I'm on my way**: 15 / 30 / 60 min chips.
- **Reminders**: list of (days, time) + add/remove.
- **Quiet hours**.
- **Send test notification**.

### 2.3 Stats (`#/stats`): Phase 7
- Today line chart (free over time) vs a "typical" band (median ± IQR for this weekday).
- Busiest-hours heatmap (weekday × hour, average free).
- "Usually ~N free at HH:MM" card.

### 2.4 Admin (`#/admin`): Phase 7, lazy chunk
Login → camera list (state, fps, last frame age) → camera detail (annotated snapshot, *Edit slots/lines* (editor), *Save reference frame*) → zone corrections (number input + note) → corrections log.

### 2.5 Privacy (`#/privacy`): Phase 8
Static text: what is processed, what's stored, location never leaves the phone.

## 3. Live data (`src/api/live.ts` + `useLiveStatus`)

```ts
type LiveState = {
  status: LotStatus | null;
  connection: 'connecting' | 'live' | 'polling' | 'offline' | 'error';
  lastMessageAt: number | null;   // Date.now() of last event/poll
  error: ApiError | null;
};
```

Behaviour:
1. On start: `GET /api/status` (fast first paint), then open `EventSource(API_BASE + '/api/stream')`.
2. `status` event → replace `status`, `connection = 'live'`.
3. `onerror`: EventSource retries by itself (server sends `retry: 3000`). If there's no event or ping for **30 s**, close it and switch to **polling** `/api/status` every 10 s, while trying SSE again every 60 s.
4. `visibilitychange` → hidden: close SSE (saves battery). Visible: fetch `/api/status` at once, then reopen SSE.
5. `online`/`offline` events update `connection`.
6. One shared connection for the whole app (module singleton), exposed through `useSyncExternalStore`.

**Mock mode:** if `VITE_API_BASE === 'mock'`, `mock.ts` produces a realistic `LotStatus` that changes every 3–8 s (random walk within capacity, sometimes stale, sometimes low confidence). Used for UI work before the backend exists and in Playwright tests.

## 4. Styling and accessibility

Tokens in `styles/index.css`:
```css
:root {
  --bg: #ffffff; --surface: #f4f5f7; --text: #111418; --muted: #5b6470;
  --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --surface: #161b22; --text: #e6edf3; --muted: #8b949e;
          --ok: #3fb950; --warn: #d29922; --bad: #f85149; --accent: #58a6ff; }
}
```
- The level is **always shown as a word**, not just a colour (colour-blind users, bright sunlight).
- Big count: ≥ 72 px, tabular numbers. Body ≥ 16 px. Touch targets ≥ 44×44 px.
- Contrast AA or better in both themes. Respects `prefers-reduced-motion` (no number animations).
- The count region is `aria-live="polite"` so screen readers announce changes, at most once every 30 s.
- Works from 320 px wide, no horizontal scroll; safe-area insets for notched phones.

## 5. PWA

`vite.config.ts` (abridged):
```ts
VitePWA({
  strategies: 'injectManifest',
  srcDir: 'src', filename: 'sw.ts',
  registerType: 'autoUpdate',
  manifest: {
    name: 'Parking', short_name: 'Parking',
    start_url: '/parking-app/#/', scope: '/parking-app/',
    display: 'standalone', background_color: '#0d1117', theme_color: '#0d1117',
    icons: [ { src: 'icons/192.png', sizes: '192x192', type: 'image/png' },
             { src: 'icons/512.png', sizes: '512x512', type: 'image/png' },
             { src: 'icons/maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' } ]
  }
})
```

`src/sw.ts`:
- `precacheAndRoute(self.__WB_MANIFEST)`: the app shell works offline.
- **Never cache** `/api/*` responses (live data must not be stale-from-cache).
- `push` event → `showNotification(title, { body, tag, data: { url }, icon, badge })`.
- `notificationclick` → focus an open app window or `clients.openWindow(url)`.
- `pushsubscriptionchange` → re-subscribe and POST the new subscription.

Install UX:
- Android/desktop: capture `beforeinstallprompt`, show an "Install app" button in the header menu.
- iPhone: detect iOS Safari and not `display-mode: standalone` → show InstallHint (needed for push).

## 6. Configuration and builds

| Env | Value | Where |
|-----|-------|-------|
| `VITE_BASE` | `/parking-app/` (preview) / `/` | build environment |
| `VITE_API_BASE` | `mock` (dev default) / `http://localhost:8000` / dev API URL (preview) / production API URL | `.env.development`, GitHub repo variable for the preview, production build settings |

Scripts: `npm run dev`, `npm run build`, `npm run preview`, `npm run test`, `npm run e2e`, `npm run lint`.

Budget: initial JS ≤ 150 KB gzip (Live screen). Stats and Admin are lazy chunks.
