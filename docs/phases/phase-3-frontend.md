# Phase 3: Mobile web app and public access

**Goal:** a phone, anywhere, shows the live counts at the **preview** URL (GitHub Pages, served from the dev Pi's API), and the app can be installed to the home screen. Production hosting comes in Phase 8.
**Needs hardware:** no (uses the Phase 2 replay feed, or mock mode).
**Specs used:** [frontend.md](../design/frontend.md), [api.md §1–3](../design/api.md), [deployment.md §5–6](../design/deployment.md).

## Deliverables
- Live screen with all states
- PWA (installable, offline shell)
- Preview deployed to GitHub Pages by Actions; dev API reachable over HTTPS while testing
- Playwright mobile tests + Lighthouse ≥ 90

**Tip:** P3.1–P3.7 can be built entirely in **mock mode** (`VITE_API_BASE=mock`), even before Phase 2 is finished.

---

## P3.1: App shell, routing, theme
**Files:** `src/main.tsx`, `src/App.tsx`, `src/styles/index.css`, `src/components/BottomNav.tsx`, `src/components/Header.tsx`

**Steps**
1. `HashRouter` with routes `/` (Live), `/alerts`, `/stats` (placeholders for now), `/privacy`, `/admin/*` (placeholder).
2. Layout: a sticky header (name + connection dot), content area, bottom nav (Live · Alerts · Stats) with icons and labels, and safe-area padding.
3. Colour tokens and dark mode per [frontend.md §4](../design/frontend.md#4-styling-and-accessibility). Expose them to Tailwind via `@theme`.

**Done when:** navigation works on a 320 px-wide viewport with no horizontal scroll, in both themes.

## P3.2: API types, client, mock
**Files:** `src/api/types.ts`, `src/api/client.ts`, `src/api/mock.ts`, `src/api/__fixtures__/*.json`

**Steps**
1. `types.ts`: `LotStatus`, `ZoneStatus`, `Level`, `Trend`, `LotInfo`, `ApiError`, copied faithfully from [api.md](../design/api.md#lotstatus).
2. `client.ts`: `API_BASE` from `import.meta.env.VITE_API_BASE`; `getStatus()`, `getLot()`; parse the error format; a 10 s timeout via `AbortController`.
3. `mock.ts`: `createMockFeed()` producing a realistic `LotStatus` stream (random walk, trend, sometimes `stale`, sometimes low confidence, sometimes `unavailable`). Same interface as the real live manager.
4. Copy JSON fixtures from the backend's tests so both sides share examples (`backend/tests/fixtures/api/`, created in P3.2 because the backend had no response examples yet; both sides test them, see [testing.md §3](../design/testing.md#3-fixtures-public-repo-safe)).

**Done when:** a unit test validates the fixtures against the TS types (a typed import + a runtime shape check).

## P3.3: Live connection manager and `useLiveStatus`
**Files:** `src/api/live.ts`, `src/hooks/useLiveStatus.ts`, `src/hooks/useNow.ts`, tests

**Steps:** implement exactly [frontend.md §3](../design/frontend.md#3-live-data-srcapilivets--uselivestatus):
- the initial `GET /api/status`, then `EventSource`
- watchdog: 30 s with no message → polling every 10 s, SSE retried every 60 s
- `visibilitychange` and `online`/`offline` handling
- a singleton store + `useSyncExternalStore`

Tests with a fake `EventSource` class and fake timers: live → silent 30 s → polling → SSE back → live; hidden → closed; visible → refetch.

**Done when:** tests pass, and with `VITE_API_BASE=http://<pi>:8000` the dev app updates live from the Phase 2 stack.

## P3.4: Live screen components
**Files:** `src/screens/LiveScreen.tsx`, `src/components/{BigCount,ZoneCard,LevelBar,TrendIcon,UpdatedAgo}.tsx`, `src/lib/status.ts`

**Steps**
1. `status.ts`: level → i18n key + colour token; `formatFree(zone)` adds "≈" when confidence < 0.8.
2. `BigCount`: the total free count, the level word, a bar. `aria-live="polite"`, throttled to 30 s.
3. `ZoneCard`: name, `free / capacity`, a bar, the level word, a trend arrow with a text alternative, and an "Estimated" note when needed.
4. `UpdatedAgo`: "Updated 3 s ago" using `Intl.RelativeTimeFormat`, re-rendering every second via `useNow`.
5. Component tests for each level, ≈, and trend.

**Done when:** it matches the [wireframe](../design/frontend.md#21-live-) in mock mode.

## P3.5: Banners and edge states
**Files:** `src/components/StatusBanner.tsx`

Implement the state table in [frontend.md §2.1](../design/frontend.md#21-live-) with priority order: `offline > server_unreachable > unavailable > stale`, plus the loading skeleton. Old numbers are greyed (not hidden) when offline, unreachable or stale.

**Done when:** each state can be forced via mock-mode query params (`?mock=offline`, `?mock=stale`, …) and looks right.

## P3.6: i18n
**Files:** `src/i18n/index.ts`, `src/i18n/locales/en.json`

**Steps:** i18next with `en` as the fallback; detect from `navigator.language`; every visible string uses `t()`; plurals ("1 free space" / "23 free spaces") via i18next plural keys. Add other languages once decided (open question #9).

**Done when:** grepping `src/` for user-visible string literals in JSX finds none (add an ESLint rule such as `i18next/no-literal-string` in `jsx-only` mode).

## P3.7: PWA
**Files:** `vite.config.ts`, `src/sw.ts`, `public/icons/*`, `src/components/InstallHint.tsx`

**Steps**
1. Icons: 192, 512, maskable 512 (a simple "P" on a dark background is fine), plus `apple-touch-icon` 180.
2. vite-plugin-pwa `injectManifest` config per [frontend.md §5](../design/frontend.md#5-pwa).
3. `sw.ts`: precache, **no caching of `/api/*`**, and `notificationclick` (push handlers are added in Phase 6).
4. Install UX: a `beforeinstallprompt` button (Android/desktop); `InstallHint` for iOS Safari when not standalone.
5. iOS meta tags in `index.html`: `apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style`, `theme-color`.

**Done when:** Chrome DevTools → Application shows a valid manifest and an active service worker; the app opens with no network and shows the shell + offline banner.

## P3.8: Deploy the preview to GitHub Pages with Actions
**Files:** `.github/workflows/pages.yml`, remove root `index.html`

**Steps**
1. Add the preview workflow from [deployment.md §6](../design/deployment.md#6-frontend-hosting). Read `VITE_BASE` in `vite.config.ts` so other hosts can use a different base path later.
2. `gh variable set API_BASE --body mock` (until P3.9 is done).
3. `gh api -X PUT repos/iulian-redinciuc/parking-app/pages -f build_type=workflow`.
4. Delete the placeholder `index.html` at the repo root in the same PR.
5. Merge → watch the Actions run → open the site on your phone.

**Done when:** the Pages URL serves the new app (in mock mode) and installs on a phone.

## P3.9: Make the dev API reachable over HTTPS (for phone testing)
**Files:** `deploy/docker-compose.yml` (`tunnel` service, `public` profile), `deploy/.env`

**Steps**
1. Use the `parking-tunnel` container (Option A in [deployment.md §5](../design/deployment.md#5-public-access-for-the-api)) with a **dev** hostname, e.g. `parking-api-dev.<domain>`. Bring it up only while testing (`--profile public up -d`), and take it down afterwards.
2. Set `CORS_ORIGINS=https://iulian-redinciuc.github.io` and restart the API.
3. Check from mobile data (not Wi-Fi): `https://<api>/healthz`.
4. Check SSE through the tunnel: `curl -N https://<api>/api/stream` keeps streaming for > 2 min (pings arrive).
5. `gh variable set API_BASE --body https://<api>` → re-run the Pages workflow.

**Done when:** the preview app on your phone over mobile data shows the live replay-feed numbers from the dev Pi.

## P3.10: Tests and quality gates
**Files:** `frontend/e2e/*.spec.ts`, `frontend/playwright.config.ts`, `.github/workflows/ci.yml` (Playwright + Lighthouse CI)

**Steps**
1. Playwright projects `iPhone 13` (webkit) and `Pixel 7` (chromium), with `VITE_API_BASE=mock` and `npm run preview`.
2. Specs: the live screen shows numbers; a count change is reflected; the offline banner shows with `context.setOffline(true)`; no horizontal scroll at 320 px; the bottom nav works.
3. Lighthouse CI (`@lhci/cli`) on the preview build: assertions accessibility ≥ 90, performance ≥ 90 (mobile). Lighthouse 12 removed the PWA category, so "installable" is asserted by `e2e/pwa.spec.ts` instead.
4. Go through the manual checklist in [testing.md §6](../design/testing.md#6-manual-release-checklist-from-phase-3-on).

**Done when:** CI is green with the e2e and Lighthouse jobs, and the manual checklist is done.

---

## Exit criteria
- [ ] On your phone (mobile data), the preview URL shows live numbers that change when the replay feed changes
- [ ] Installable on Android and iPhone; opens offline with the shell + banner
- [ ] API down → banner within 30 s → recovers by itself
- [ ] Playwright + Lighthouse green in CI
