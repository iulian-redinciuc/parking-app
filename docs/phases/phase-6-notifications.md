# Phase 6: Notifications

**Goal:** people get told how many spaces are free when they're heading to or approaching the lot.
**Needs hardware:** no (can run in parallel with Phases 4–5 once Phase 3 is done).
**Specs used:** [notifications.md](../design/notifications.md), [api.md §2, §6](../design/api.md), [data-model.md](../design/data-model.md#push_subscription), [frontend.md §2.2, §5](../design/frontend.md).

## Deliverables
- Web Push working on Android and iPhone (installed PWA)
- Tiers 1–4 from [notifications.md](../design/notifications.md#1-the-constraint)
- The Alerts screen

---

## P6.1: VAPID keys and push sender
**Files:** `parking/push/sender.py`, CLI `push vapid-keys`, tests

**Steps**
1. `parking push vapid-keys` generates a P-256 key pair (`py_vapid` comes with pywebpush) and prints `VAPID_PUBLIC_KEY` (URL-safe base64, uncompressed point) and `VAPID_PRIVATE_KEY`. Put them in `.env`. **Back them up**: losing them breaks every subscription.
2. `PushSender.send(sub, payload, urgency) -> Result` per [notifications.md §2](../design/notifications.md#2-web-push-basics): TTL 600; 404/410 → delete; other errors → failures++ (delete at 5); writes `notification_log`.
3. `send_many(subs, payload)` with a thread pool of 10.
4. Tests with `webpush` mocked: success, 410 deletes, 500 increments.

**Done when:** tests pass.

## P6.2: Subscription storage and endpoints
**Files:** `parking/db/models.py` (+ migration), `parking/api/routes/push.py`, tests

**Steps**
1. Tables `push_subscription`, `notification_log` ([data-model.md](../design/data-model.md#push_subscription)).
2. Endpoints from [api.md §2](../design/api.md#2-public-rest-endpoints): `vapid-public-key`, `POST/PATCH/DELETE subscriptions`, `on-my-way`, `test`. Validate `Prefs` (radius 100–5000, times `HH:MM`, a valid IANA tz via `zoneinfo`).
3. Rate limits per [api.md §7](../design/api.md#7-cross-cutting).
4. Tests: upsert by endpoint, validation errors, delete, the test-push rate limit.

**Done when:** tests pass.

## P6.3: Service worker push handling
**Files:** `frontend/src/sw.ts`

**Steps:** add the `push`, `notificationclick` and `pushsubscriptionchange` handlers per [frontend.md §5](../design/frontend.md#5-pwa). Icon + badge (monochrome 96 px). `renotify: false`, so updates to the same tag replace the notification quietly.

**Done when:** DevTools → Application → Service Workers → "Push" with a test payload shows a notification, and tapping it opens or focuses the app.

## P6.4: Alerts screen and permission flow
**Files:** `frontend/src/screens/NotificationsScreen.tsx`, `frontend/src/lib/push.ts`, `frontend/src/components/InstallHint.tsx`

**Steps**
1. States: `unsupported` (no `PushManager`) → message; `ios-not-installed` → InstallHint; `default` → "Enable notifications" button; `denied` → how to re-enable in settings; `granted+subscribed` → settings.
2. `push.ts`: `subscribe(prefs)`, `updatePrefs(prefs)`, `unsubscribe()`, `isSubscribed()` per [notifications.md §2](../design/notifications.md#2-web-push-basics).
3. Settings UI per [frontend.md §2.2](../design/frontend.md#22-alerts-alerts-phase-6). Save changes with PATCH (debounced 500 ms).
4. "Send test notification" button.

**Done when:** on Android Chrome, enable → test → a notification arrives.

## P6.5: Tier 1, proximity while open
**Files:** `frontend/src/hooks/useProximity.ts`, `frontend/src/lib/geo.ts`, `frontend/src/components/ProximityBanner.tsx`, tests

**Steps:** implement [notifications.md §3](../design/notifications.md#3-tier-1-proximity-while-open-hooksuseproximityts). Haversine unit tests (known city-pair distances). Hook tests with a mocked `navigator.geolocation` (outside → inside → banner once → cooldown).

**Done when:** in Chrome DevTools → Sensors → a custom location near the lot, the banner + local notification show once.

## P6.6: Tier 2, "I'm on my way"
**Files:** `parking/push/rules.py`, hooks in the consumer's change handler, frontend buttons

**Steps**
1. `rules.should_send_on_my_way(sub, old_status, new_status, now) -> bool` implementing [notifications.md §4](../design/notifications.md#4-tier-2-im-on-my-way-server-rules-pushrulespy).
2. On every status change: query the active on-my-way subscriptions → filter by the rules → `send_many`. Update `last_sent_*`.
3. Frontend: 15/30/60 min chips, a visible countdown, and a cancel button (`minutes: 0`).
4. Unit tests for every rule with a fake clock.

**Done when:** start "on my way" for 15 min, change the replay feed → pushes arrive per the rules and stop after 15 min.

## P6.7: Tier 3, schedules, quiet hours and almost-full alerts
**Files:** `parking/push/scheduler.py`, `parking/push/rules.py`

**Steps**
1. An APScheduler `AsyncIOScheduler` started in the API lifespan; a minutely job checks due schedules per subscription timezone ([notifications.md §5](../design/notifications.md#5-tier-3-scheduled-reminders)).
2. Quiet hours that span midnight (22:00–07:00).
3. Almost-full alerts on level transitions, with a 2 h cooldown per subscription.
4. Tests with a fake clock across timezones and DST changes (e.g. `Europe/Bucharest` on the last Sunday of October).

**Done when:** a schedule set 2 minutes ahead fires once; nothing fires in quiet hours.

## P6.8: Tier 4, Home Assistant
**Files:** `parking/api/ha_bridge.py`, `docs/home-assistant.md` (copy of the automation example)

**Steps**
1. If `HA_MQTT_URL` is set: connect to the home broker, publish the discovery configs (retained) at start-up and `parking/main/status` (retained) on every change, per [notifications.md §6](../design/notifications.md#6-tier-4-home-assistant).
2. A failure to reach the home broker must **never** affect the main pipeline (a separate task, with its own reconnect loop).
3. In HA: create zone `zone.parking` around the lot and add the automation.

**Done when:** HA shows `sensor.parking_free_spaces` updating, and a real drive into the zone triggers the notification on your phone.

## P6.9: Device test matrix
**Steps:** fill in the [test matrix](../design/notifications.md#8-test-matrix-phase-6) on a real Android phone, a real iPhone (installed PWA) and a desktop browser. Note the OS and browser versions in PROGRESS.md.

**Done when:** every "expected ✅" cell is confirmed.

---

## Exit criteria
- [ ] Push works on Android and iPhone (installed)
- [ ] A drive test: Tier 1 banner (app open) and Tier 4 HA notification (app closed) both fire near the lot
- [ ] "On my way" and schedules behave per the rules
- [ ] No permission prompt appears without a tap
