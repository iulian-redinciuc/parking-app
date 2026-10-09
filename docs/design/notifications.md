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

## 5. Tier 3: scheduled reminders

- `prefs.schedules: [{days:[1..5], time:"08:30"}]` in the subscription's `tz`.
- APScheduler job every minute: for each subscription with a schedule due this minute (in its timezone) → send the status push (`kind: schedule`).
- Skip during quiet hours. Skip if already sent within the last 10 min.

### "Almost full" alerts (`prefs.alert_when_almost_full`)
- When a preferred zone's level becomes `almost_full` or `full`, push to subscribers with this pref, at most once per 2 h per subscription, respecting quiet hours.

## 6. Test matrix (Phase 6)

| Device | Browser | Install? | Push | Tier 1 | Tier 2 | Tier 3 |
|--------|---------|----------|------|--------|--------|--------|
| Android phone | Chrome | optional | ✅ expected | ✅ | ✅ | ✅ |
| iPhone (iOS ≥ 16.4) | Safari, installed to home screen | **required** | ✅ expected | ✅ | ✅ | ✅ |
| iPhone | Safari, not installed | — | ❌ (show install hint) | ✅ (in-app banner only) | ❌ | ❌ |
| Desktop | Chrome / Firefox | no | ✅ | n/a | ✅ | ✅ |
