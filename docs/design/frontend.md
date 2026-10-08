# Frontend (mobile-first PWA)

## 1. Stack

| Concern | Choice | Notes |
|---------|--------|-------|
| Build | **Vite** | `base` from `VITE_BASE` (`/parking-app/` for the GitHub Pages preview, usually `/` in production) |
| UI | **React 19 + TypeScript** (strict) | |
| Routing | **react-router `HashRouter`** | Works on any static host without server rewrite rules (GitHub Pages has none), so `#/stats` never 404s on reload |
| Styling | **Tailwind CSS v4** + CSS custom properties for tokens | Light and dark |
| i18n | **i18next + react-i18next** | `en` first; files in `src/i18n/locales/*.json` (§7) |
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

Priority `offline > server_unreachable > unavailable > stale`; `estimated` is never a banner. The pure rules are `liveView()` in `src/lib/banner.ts` (P3.5), drawn by `StatusBanner` (words + icon, `role="status"`) and `LiveSkeleton`: `server_unreachable` = `connection` is `error` (or not `live` with a non-503 `error`) and nothing heard for > 20 s since `lastMessageAt`, or since the app started if nothing ever arrived (until then the last numbers or the skeleton stay as they are). Stale minutes = age of the oldest stale zone's `updated_at` on the client clock, whole minutes, at least 1, with the stale zone names under it ("No camera data yet" if a stale zone was never fed); stale zone cards also say "No fresh camera data: last known numbers". **Greyed** = the `.dimmed` class (text in `--muted`, which keeps AA contrast, and the bar fill muted), never opacity: the whole screen when offline/unreachable, only the stale zone cards when stale.

Components (P3.4, `src/components/`, helpers in `src/lib/status.ts`): **BigCount** (total free, "free space(s)", `LevelBar` + level word; the visible number updates at once, a screen-reader-only `aria-live="polite"` copy is throttled to one change per 30 s), **ZoneCard** (name, `free / capacity`, `TrendIcon`, `LevelBar` + level word, "ⓘ Estimated from entry/exit counts" for `flow` zones or "Estimated: the camera view is unclear" otherwise when confidence < 0.8), **LevelBar** (taken share `occupied / capacity`, coloured by level, decorative), **TrendIcon** (arrow for free spaces: `filling` ↘, `emptying` ↗, `steady` →, with the text "Trend: getting fuller / emptying / steady"), **UpdatedAgo** ("Updated 3 seconds ago" via `Intl.RelativeTimeFormat`, from `lastMessageAt`, i.e. the last event, ping or poll, ticking with `useNow`). Level words and colours are the [api.md table](api.md#levels).

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
3. `onerror`: EventSource retries by itself (server sends `retry: 3000`). If there's no event or ping for **30 s**, close it and switch to **polling** `/api/status` every 10 s, while trying SSE again every 60 s. The heartbeat is the server's `event: ping` (a `: ping` comment would be invisible to `EventSource`, api.md §3). Any sign of life from the stream (`open`, `ping`, `status`) re-arms the watchdog, sets `connection = 'live'` and stops polling; a source that ends up `CLOSED` (e.g. a 503 instead of a stream; the browser won't retry) falls back to polling at once. `lastMessageAt` is set by `status` events, pings and successful fetches.
4. `visibilitychange` → hidden: close SSE (saves battery) and stop polling; in-flight fetches are aborted. Visible: fetch `/api/status` at once, then reopen SSE.
5. `online`/`offline` events update `connection` (`offline` also closes everything; `online` reconnects as on start). A failed fetch (`network`/`timeout`/…) sets `error` and `connection = 'error'` unless the stream is live, keeping the last `status`; the next good poll goes back to `polling`. `503 unavailable` sets `status: null` + `error` but leaves `connection` alone (the server answered).
6. One shared connection for the whole app (module singleton `liveFeed()` in `live.ts`, the mock feed in mock mode), exposed through `useSyncExternalStore` by `useLiveStatus()` (starts it on first use, never stops it). `useNow(intervalMs = 1000)` re-renders with `Date.now()` for "updated 12 s ago".

The live manager and the mock share one interface (`src/api/types.ts`): `LiveFeed` = `getSnapshot(): LiveState`, `subscribe(listener): unsubscribe` (ready for `useSyncExternalStore`), `start()`, `stop()`. REST calls go through `src/api/client.ts` (`getStatus()`, `getLot()`, 10 s timeout); every failure is an `ApiRequestError` whose `error: ApiError` has the server's `code`/`message`/`details` plus `status` and `retryAfter`, or a client code `network` / `timeout` / `bad_response` (bodies are shape-checked by `src/api/validate.ts`).

**Mock mode:** if `VITE_API_BASE === 'mock'` (also when unset), `mock.ts` `createMockFeed()` produces a realistic `LotStatus` that changes every 3–8 s (a random walk within capacity whose drift itself wanders, so trends appear; levels and trends use the server's rules; episodes of 2–4 updates: a zone stale (counts frozen) ~6 % of starts, low confidence 0.4–0.7 ~8 %, and rarely the whole lot `unavailable` = `status: null`, `error.code = 'unavailable'`, like a 503). `getStatus()`/`getLot()` return mock data too. The lot is the `lot-info.json` fixture (Ground 40 slots, Underground 60 flow). `seed` makes it repeatable. Used for UI work before the backend exists and in Playwright tests. **`?mock=<scenario>`** (before the `#`, e.g. `/parking-app/?mock=stale#/`) forces an edge state with the random episodes off: `loading` (never answers), `offline`, `unreachable` (alias `server_unreachable`; last status, `connection 'error'`, last message 60 s ago), `unavailable` (503), `stale` (first zone frozen 5 min back), `estimated` (last zone at confidence 0.6). The mock feed also follows the browser's `offline`/`online` events.

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

As built (P3.7): `start_url`/`scope` follow `VITE_BASE` (`${base}#/`, `base`). Icons come from `public/icons/icon.svg` (a path-drawn "P", inside the maskable safe zone) via `node scripts/icons.mjs` (Playwright Chromium; `PW_CHROMIUM_PATH` for a system one): `192.png`, `512.png`, `maskable-512.png`, `apple-touch-icon-180.png`. `sw.ts`: `skipWaiting` + `clientsClaim` (autoUpdate), `cleanupOutdatedCaches`, `precacheAndRoute`, a `NavigationRoute` to the cached `index.html` with `/api/` denied, and **no runtime caching at all**; `notificationclick` focuses an app window in scope (navigating it to `data.url`) or opens one, and only follows same-origin URLs (`notificationUrl()` in `src/lib/pwa.ts`). `push`/`pushsubscriptionchange` come in Phase 6. `beforeinstallprompt` is captured from app start (`src/lib/install.ts`); the header has no menu yet, so the button sits **in the header** (icon-only below 640 px, name always "Install app") and hides after use or `appinstalled`. **InstallHint** (`src/components/InstallHint.tsx`) shows on the Live screen under the numbers for iOS Safari (incl. iPadOS) when not standalone, dismissible (remembered in `localStorage`); `<InstallHint always />` (no close button) is for the Alerts screen in Phase 6. `index.html` has `apple-mobile-web-app-capable`, `mobile-web-app-capable`, status bar `black-translucent` (the header already pads for the safe area), `apple-mobile-web-app-title` and the `apple-touch-icon`. The mock feed starts offline when the browser is and sends nothing while offline, like the real feed.

## 6. Configuration and builds

| Env | Value | Where |
|-----|-------|-------|
| `VITE_BASE` | `/parking-app/` (preview) / `/` | build environment |
| `VITE_API_BASE` | `mock` (dev default) / `http://localhost:8000` / dev API URL (preview) / production API URL | `.env.development`, GitHub repo variable for the preview, production build settings |

Scripts: `npm run dev`, `npm run build`, `npm run preview`, `npm run test`, `npm run e2e`, `npm run lint`.

Budget: initial JS ≤ 150 KB gzip (Live screen). Stats and Admin are lazy chunks.

## 7. Languages (i18n)

- `src/i18n/index.ts` initialises i18next once (imported by `main.tsx` and the test setup) with the locale files **bundled** and `initAsync: false`, so `t()` works on the first render and in plain helpers (`liveView`, `levelInfo`, `formatUpdatedAgo` use the exported `t`; components call `useTranslation()`).
- **Language** = the first entry of `navigator.languages` whose primary subtag (`ro-RO` → `ro`) has a locale file, else **`en`** (also the fallback for missing keys). `<html lang>` follows it, and the live feed asks the API for zone names in it (`?lang=`, api.md §1).
- **Languages:** only `en` until open question #9 is answered. Adding one = `locales/<lang>.json` with the same keys + one line in `RESOURCES`.
- **Plurals** use i18next plural keys (`count.free_one` "free space" / `count.free_other` "free spaces", `banner.stale_one/_other`), called with `{ count }`. Relative times ("Updated 3 seconds ago") come from `Intl.RelativeTimeFormat` in the UI language inside the `updated.ago` key.
- Keys are grouped by place: `app`, `connection.<state>`, `nav`, `screen`, `count`, `level.<level>`, `trend.<trend>`, `zone`, `updated`, `banner`, `loading`.
- **Lint:** `eslint-plugin-i18next` `no-literal-string` in `jsx-only` mode on `src/**/*.tsx` (tests excluded), also for the `alt`, `aria-label`, `placeholder` and `title` attributes; the symbols `≈ ⓘ /` are allowed. A visible string literal in JSX fails `npm run lint`.
