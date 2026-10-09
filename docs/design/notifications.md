# Notifications

## 1. The constraint

**A web page cannot read the phone's location while it is closed or in the background.** That's true for Chrome on Android and Safari on iPhone. So "notify me automatically when I drive near the lot, without opening anything" is impossible for a web app, and this project is **web only** (no native or app-store app). The three tiers below get as close as possible, and the app explains the limitation to users.

| Tier | Trigger | Works with app closed? | Who | Phase |
|------|---------|-----------------------|-----|-------|
| 1 | Phone is within the radius **while the app is open** | No | Everyone | 6 |
| 2 | User tapped **"I'm on my way"** | ✅ (server push) | Everyone with push | 6 |
| 3 | **Scheduled reminder** (e.g. weekdays 08:30) | ✅ | Everyone with push | 6 |

## 2. Web Push basics

- Keys: `parking push vapid-keys` prints `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` for `.env`. **Never rotate casually**: rotating invalidates every subscription.
  - Formats (unpadded URL-safe base64): the public key is the 65-byte uncompressed P-256 point (the browser's `applicationServerKey`), the private key the raw 32-byte scalar (`py_vapid.Vapid.from_string` reads it). The command prints both lines to stdout and a back-up reminder to stderr.
- Subscribe in the browser (`lib/push.ts`):
  1. `Notification.requestPermission()`, only inside the click handler of "Enable notifications".
  2. `reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) })`, where `key` comes from `GET /api/push/vapid-public-key`.
  3. `POST /api/push/subscriptions` with the subscription JSON, prefs, `Intl.DateTimeFormat().resolvedOptions().timeZone` and the UI language.
  4. Keep `endpoint` in `localStorage` (wrapped in try/catch) to reference this subscription later.
  5. `savePushRegistration({prefs, tz, lang})` (`lib/swPush.ts`) after every successful POST/PATCH, `clearPushRegistration()` on unsubscribe: the service worker reads it on `pushsubscriptionchange` (frontend.md §5).
- As built (P6.4): `subscribe`, `updatePrefs`, `unsubscribe`, `isSubscribed`, `sendTest` in `lib/push.ts`; the screen's states and settings are in [frontend.md §2.2](frontend.md#22-alerts-alerts-phase-6).
- Send (`push/sender.py`): `pywebpush.webpush(subscription_info, json.dumps(payload), vapid_private_key, vapid_claims={"sub": VAPID_SUBJECT}, ttl=600)`.
  - TTL 600 s: a "12 free" message is worthless an hour later.
  - Urgency header `high` for on-my-way and almost-full; `normal` for schedules.
  - 404/410 → delete the subscription. Other errors → `failures += 1`, delete at 5.
  - Send in a thread pool (pywebpush is blocking), max 10 at a time.
  - API: `PushSender(engine, vapid_private_key, vapid_subject, clock=, webpush=)` (or `PushSender.from_settings(engine, settings)`); `send(sub, payload, urgency) -> SendResult(subscription_id, ok, status_code, deleted, error)` and `send_many(subs, payload, urgency) -> list[SendResult]` (input order). `webpush` is injectable for tests. The private key is parsed once at construction (a missing key or subject raises `ValueError`).
  - Every attempt writes a `notification_log` row (`kind` from the payload, `status` `sent`/`failed`, error truncated to 500 chars). Success resets `failures` to 0 and sets `last_sent_at`; `last_sent_free`/`last_sent_level` are left to the rules (P6.6–P6.7). Network errors (no HTTP status) count as failures.
- **Payload:** see [api.md §6](api.md#6-push-notification-payload-web-push-encrypted-by-pywebpush). `tag: "parking-status"` replaces the previous notification instead of piling up.

### iPhone specifics
- Web Push works only on **iOS/iPadOS 16.4+** and **only after "Add to Home Screen"**, with the app opened from that icon.
- Permission must be requested from a user tap inside the installed app.
- The UI detects iOS + not standalone and shows install instructions instead of the enable button.

## 3. Tier 1: proximity while open (`hooks/useProximity.ts`)

1. Only active if the user enabled *Tell me when I'm near* (stored locally) **and** granted location permission.
2. `navigator.geolocation.watchPosition(cb, err, { enableHighAccuracy: false, maximumAge: 60_000, timeout: 30_000 })` while the page is visible. Stop on `visibilitychange → hidden`.
3. Distance = haversine(position, `lot.location`) from `GET /api/lot`. **The calculation happens on the phone; position is never sent.**
4. Entering the radius (distance < `radius_m` and the previous reading was outside, or there was no previous reading):
   - In-app banner: "You're 400 m away · 23 free (Ground 12 · Underground ≈11)".
   - Local notification via `registration.showNotification` (same tag), if permission is granted.
5. Cooldown: no repeat for 2 h (timestamp in `localStorage`).
6. Ignore readings with `accuracy` > 1000 m.

As built (P6.5): `lib/geo.ts` (`haversineM` with the mean Earth radius 6 371 008.8 m, `proximityStep` = steps 4–6 as a pure function, `roundDistance`: 10 m steps below 1 km, then 0.1 km; `locationAllowed` / `requestLocation`; the cooldown in `localStorage` `parking.proximityAlertAt`), `hooks/useProximity.ts` (the watch; the last reading survives hiding the page, so driving in while it was hidden still counts as entering; a `PERMISSION_DENIED` error stops the watch, timeouts don't), `components/ProximityBanner.tsx` (top of every screen, dismissible; the local notification uses the push payload shape with `kind: "proximity"` and the `parking-status` tag). The switch lives on the Alerts screen and **asks for the location from that tap**; the watch starts only when `navigator.permissions` says `granted` (or, where it can't say, when that tap's request succeeded). Without push on the device (not enabled, blocked, unsupported, iPhone Safari outside the installed app) the screen still shows *Tell me when I'm near* + radius, kept on the device only (`saveLocalPrefs`) and sent with the other prefs once push is enabled; turning push off keeps them. The radius is `prefs.radius_m`, else the lot's `notify_radius_m`.

## 4. Tier 2: "I'm on my way" (server rules, `push/rules.py`)

`POST /api/push/on-my-way {endpoint, minutes}` sets `on_my_way_until = now + minutes`, and sends one push immediately with the current status.

After that, on every status change, for each subscription with `on_my_way_until > now`:

| Send if any of… | Details |
|-----------------|---------|
| Level changed | e.g. `plenty → filling`, `almost_full → full` |
| Free changed a lot | `abs(free − last_sent_free) ≥ max(3, 10% of capacity)` (for the zones in `prefs.zones`) |
| A preferred zone became full | always, even if the above doesn't hold |

…and all of these hold:
- ≥ 2 min since `last_sent_at` (except "became full", which ignores the gap),
- ≤ 6 pushes per on-my-way window,
- not in quiet hours (on-my-way **overrides** quiet hours, since the user explicitly asked).

As built (P6.6): `parking/push/rules.py` `should_send_on_my_way(sub, old_status, new_status, now, levels)`. **Watched** = the zones in `prefs.zones` (all zones when unset): their summed free count, and the level of that sum by the lot's `api.levels` thresholds, are what `last_sent_free` / `last_sent_level` hold, so "free changed a lot" is measured against the **last push**, not the previous status, and the 10 % is of the watched capacity. A last push without data (`No data yet`) sends again as soon as data arrives (after the 2 min gap). "Became full" compares `old_status` → `new_status` per watched zone and skips the gap but not the cap. The cap counts the immediate push: `push_subscription.on_my_way_sent` (migration `0003`) is reset by every `POST on-my-way` (a new window starts over; `minutes: 0` cancels) and incremented per delivered push. The hook: `Ingestor(…, on_status=)` gets `(previous, new)` after each published status (none at start-up restore); `parking/push/on_my_way.py` `OnMyWayNotifier` skips changes where no zone's free/level moved (trend/confidence only), otherwise queries `on_my_way_until > now`, applies the rules, `send_many`s one payload per (lang, tz, zones) with `Urgency: high`, `kind: on_my_way`, and records `last_sent_*` for the delivered ones. It runs as a task behind a lock (shared with the Tier 3 jobs since P6.7) in a worker thread, so ingest never waits for a push service; without VAPID settings there is no hook. Frontend: 15 / 30 / 60 min chips, a countdown (`role="timer"`, `m:ss left` + *Updates until* the local time) and *Stop updates*; the end time is kept in `localStorage` `parking.onMyWayUntil` so it survives a reload (`startOnMyWay` / `cancelOnMyWay` / `onMyWayUntil` in `lib/push.ts`; turning push off forgets it; a failed cancel keeps it).

## 5. Tier 3: scheduled reminders

- `prefs.schedules: [{days:[1..5], time:"08:30"}]` in the subscription's `tz`.
- APScheduler job every minute: for each subscription with a schedule due this minute (in its timezone) → send the status push (`kind: schedule`).
- Skip during quiet hours. Skip if already sent within the last 10 min.

### "Almost full" alerts (`prefs.alert_when_almost_full`)
- When a preferred zone's level becomes `almost_full` or `full`, push to subscribers with this pref, at most once per 2 h per subscription, respecting quiet hours.

As built (P6.7): rules in `parking/push/rules.py`, jobs in `parking/push/scheduler.py`.
- **Quiet hours** (`in_quiet_hours`): `{from, to}` in the subscription's `tz`, `from` included and `to` not; `from > to` spans midnight (22:00–07:00 is quiet at 22:00 and 06:59, not at 07:00); `from == to` means no quiet time. They hold back reminders and almost-full alerts, never on-my-way or the test push.
- **Reminders** (`schedule_instants`, `PushScheduler`): an APScheduler `AsyncIOScheduler` (UTC, cron `second=0`, `coalesce`, `max_instances=1`) started in the API lifespan when push is configured. Each run sends to every subscription with a reminder due in `(previous run, now]`, at most 5 min back (a run late by more drops the older reminders; the first run after start-up also looks 1 min back). A reminder is `HH:MM` on ISO weekdays `days` in the subscription's tz, turned into a UTC instant per local date: on the **autumn DST change** the repeated hour fires only on its first pass (`fold=0`); in **spring** a time inside the skipped hour fires one hour later on the wall clock (03:30 → 04:30 in `Europe/Bucharest`). Skipped when any push went to that subscription in the last 10 min (`last_sent_at`, so an on-my-way or test push counts), or in quiet hours. `Urgency: normal`, `kind: schedule`, the current status (`No data yet` before data).
- **Almost full** (`almost_full_zones`, `AlmostFullNotifier`): an ingest hook next to the on-my-way one. A watched zone (`prefs.zones`, all when unset) counts when its level gets **worse** into `almost_full` or `full` (`filling → almost_full`, `almost_full → full`, `plenty → full`; getting better, an unchanged level, a stale zone or the start-up restore don't). The 2 h cooldown is the last `almost_full` row with status `sent` in `notification_log`, so a quiet-hours skip doesn't start it. `Urgency: high`, `kind: almost_full`, the normal status payload.
- Neither touches `last_sent_free` / `last_sent_level` (the on-my-way "last push"). The two hooks and the reminder job share one lock, so their DB updates never race; prefs are JSON, so the subscriptions are filtered in Python (fine for hundreds).

### 5.1 Admin alerts (P7.8)

For the people running the lot, not the drivers. A logged-in admin turns on **Receive admin alerts** on the Alerts screen of a device with push enabled; that sets `push_subscription.admin_alerts` ([api.md §4](api.md#4-admin-endpoints)). `AdminAlertMonitor` (`parking/push/admin_alerts.py`) runs in the API every 10 s when push is configured, behind the same lock as the other push dispatches:

| Issue (`kind:<id>`) | Condition | Alert after |
|---------------------|-----------|-------------|
| `camera_down:<camera>` | the camera's latest health is `down` (the worker's own, after `stale_after_s` without a healthy frame, or the API's after 30 s of silence) | 2 min |
| `camera_shifted:<camera>` | its latest health issue is `shifted` and it isn't down | at once |
| `stale:<zone>` | the zone is stale, has had data before, and none of its cameras is down (that's already the camera alert) | 5 min |
| `clamps:<zone>` | more than 3 clamped entry/exit events (`flow_event.applied = false`) today, lot-local day, counted after the zone's last correction | at once |

- The grace time counts from the first check that saw the condition, continuously; it clearing resets it.
- **One alert per issue per hour:** `kind: admin_alert`, `Urgency: high`. While the issue lasts the alert repeats hourly (same tag, so it replaces the last one); an issue that clears and comes back within the hour of its last alert waits for that hour.
- **Resolved:** when an issue that got an alert clears, one push "…is back up / live data again / count fixed" with the time span, `Urgency: normal`, same tag. An issue that cleared before its alert sends nothing.
- Quiet hours and the drivers' prefs don't apply. Text is English; zone names follow the subscription's `lang`, times its `tz`.
- The state is in memory (per API process): after a restart an issue still there alerts again. Every attempt is a `notification_log` row like any push.
- A camera unplugged reaches the API as `down` after `stale_after_s` (60 s) plus up to one 10 s heartbeat, so the alert lands ~3 min after the unplug.

## 6. Test matrix (Phase 6)

| Device | Browser | Install? | Push | Tier 1 | Tier 2 | Tier 3 |
|--------|---------|----------|------|--------|--------|--------|
| Android phone | Chrome | optional | ✅ expected | ✅ | ✅ | ✅ |
| iPhone (iOS ≥ 16.4) | Safari, installed to home screen | **required** | ✅ expected | ✅ | ✅ | ✅ |
| iPhone | Safari, not installed | — | ❌ (show install hint) | ✅ (in-app banner only) | ❌ | ❌ |
| Desktop | Chrome / Firefox | no | ✅ | n/a | ✅ | ✅ |

### 6.1 Results (P6.8)

Fill in one row per device actually tested: ✅ confirmed, ❌ failed (note why), — not tested yet.

| Device / OS | Browser (version) | Installed? | Push | Tier 1 | Tier 2 | Tier 3 | Date | Notes |
|-------------|-------------------|------------|------|--------|--------|--------|------|-------|
| Desktop, Debian 13 (dev Pi 5, arm64) | Chromium 154.0.8037.92 (FCM) | no | ✅ | n/a | ✅ | ✅ | 2026-10-09 | Live runs against a real `parking api` with the dev VAPID keys: test push (P6.4), on-my-way rules (P6.6), reminder + quiet hours + almost-full (P6.7). Tier 1 also passes in Chromium with an emulated location (P6.5 e2e) |
| Desktop | Firefox | no | — | n/a | — | — | | Not installed on the dev Pi |
| Android phone | Chrome | optional | — | — | — | — | | Needs P3.9 (HTTPS from the phone) |
| iPhone (iOS ≥ 16.4) | Safari, installed | required | — | — | — | — | | Needs P3.9 |
| iPhone | Safari, not installed | — | — | — | — | — | | Expected: install hint, no push; Tier 1 banner only |

### 6.2 How to run it on a phone

Needs the dev API reachable over HTTPS (P3.9) and the frontend built against it (`VITE_API_BASE=https://parking-api-dev.<domain>`). For Tier 1, set `LOT_LAT`/`LOT_LON` in `deploy/.env` to where the test happens (or test near the real lot).

1. **Push:** open the app → *Alerts* → *Enable notifications* (the permission prompt must appear only after the tap) → *Send test notification* → a notification arrives with the screen locked. Tapping it opens the app.
2. **Tier 1:** turn on *Tell me when I'm near*, allow location, walk/drive into the radius with the app open → banner + (with the notification permission) one notification; not again within 2 h.
3. **Tier 2:** under *I'm on my way* tap *15 min* → a push arrives at once; change the occupancy (e.g. `parking replay` or a few posted observations) by ≥ 3 free / a level → another push (≥ 2 min apart); *Stop updates* → none.
4. **Tier 3:** add a reminder 2 min ahead → one push at that minute; one inside quiet hours → none; *Warn when almost full* on + a zone becoming almost full → one push, not again within 2 h.
5. **iPhone not installed:** *Alerts* shows the install hint instead of the enable button; Tier 1 banner still works.

Write the OS and browser versions into the table above and into PROGRESS.md.
