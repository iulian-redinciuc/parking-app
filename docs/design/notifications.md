# Notifications

## 1. The constraint

**A web page cannot read the phone's location while it is closed or in the background.** That's true for Chrome on Android and Safari on iPhone. So "notify me automatically when I drive near the lot, without opening anything" is impossible for a pure web app. The tiers below get as close as possible, and Tier 5 (native wrapper) removes the limit.

| Tier | Trigger | Works with app closed? | Who | Phase |
|------|---------|-----------------------|-----|-------|
| 1 | Phone is within the radius **while the app is open** | No | Everyone | 6 |
| 2 | User tapped **"I'm on my way"** | ✅ (server push) | Everyone with push | 6 |
| 3 | **Scheduled reminder** (e.g. weekdays 08:30) | ✅ | Everyone with push | 6 |
| 4 | **Home Assistant** zone entry (HA Companion app geofence) | ✅ | Your household | 6 |
| 5 | **Native geofence** (Capacitor app) | ✅ | Users of the native app | 9 |

## 2. Web Push basics

- Keys: `parking push vapid-keys` prints `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` for `.env`. **Never rotate casually**: rotating invalidates every subscription.
- Subscribe in the browser (`lib/push.ts`):
  1. `Notification.requestPermission()`, only inside the click handler of "Enable notifications".
  2. `reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) })`, where `key` comes from `GET /api/push/vapid-public-key`.
  3. `POST /api/push/subscriptions` with the subscription JSON, prefs, `Intl.DateTimeFormat().resolvedOptions().timeZone` and the UI language.
  4. Keep `endpoint` in `localStorage` (wrapped in try/catch) to reference this subscription later.
- Send (`push/sender.py`): `pywebpush.webpush(subscription_info, json.dumps(payload), vapid_private_key, vapid_claims={"sub": VAPID_SUBJECT}, ttl=600)`.
  - TTL 600 s: a "12 free" message is worthless an hour later.
  - Urgency header `high` for on-my-way and almost-full; `normal` for schedules.
  - 404/410 → delete the subscription. Other errors → `failures += 1`, delete at 5.
  - Send in a thread pool (pywebpush is blocking), max 10 at a time.
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

## 6. Tier 4: Home Assistant

The API's `ha_bridge.py` (enabled when `HA_MQTT_URL` is set) publishes to your **home** broker:

1. **MQTT discovery configs** (retained), once at start-up:
   - Topic `homeassistant/sensor/parking_main_total_free/config`:
     ```json
     { "name": "Parking free spaces", "unique_id": "parking_main_total_free",
       "state_topic": "parking/main/status", "value_template": "{{ value_json.total.free }}",
       "unit_of_measurement": "spaces", "icon": "mdi:parking",
       "json_attributes_topic": "parking/main/status",
       "json_attributes_template": "{{ {'level': value_json.total.level, 'stale': value_json.total.stale} | tojson }}",
       "device": { "identifiers": ["parking_main"], "name": "Parking", "manufacturer": "parking-app" } }
     ```
   - The same for each zone: `parking_main_<zone>_free` with `value_template: "{{ (value_json.zones | selectattr('id','eq','<zone>') | first).free }}"`.
2. **`parking/main/status`** retained, on every change.

Example automation (put in HA, not in this repo):
```yaml
alias: Parking - tell me free spaces when I get close
trigger:
  - platform: zone
    entity_id: person.<you>
    zone: zone.parking          # create a zone around the lot in HA, radius e.g. 500 m
    event: enter
action:
  - service: notify.mobile_app_<your_phone>
    data:
      title: "Parking: {{ states('sensor.parking_free_spaces') }} free"
      message: >
        Ground {{ states('sensor.parking_ground_free') }} ·
        Underground {{ states('sensor.parking_underground_free') }}
mode: single
```
The HA Companion app does true background geofencing on both Android and iPhone, so this is a real arrival notification today, for anyone who has HA access.

## 7. Tier 5: native app (Phase 9 summary)

- Capacitor project in `mobile/` that loads the built frontend.
- Geofencing plugin (e.g. `@capacitor-community/background-geolocation` or a dedicated geofence plugin; evaluate licences and maintenance status at the time).
- On geofence enter → fetch `/api/status` → local notification.
- Android: APK sideload or Play Store (one-time fee). iPhone: Apple Developer Program (99 USD/year) for TestFlight or the App Store; needs "Always" location permission and a justification text.

## 8. Test matrix (Phase 6)

| Device | Browser | Install? | Push | Tier 1 | Tier 2 | Tier 3 |
|--------|---------|----------|------|--------|--------|--------|
| Android phone | Chrome | optional | ✅ expected | ✅ | ✅ | ✅ |
| iPhone (iOS ≥ 16.4) | Safari, installed to home screen | **required** | ✅ expected | ✅ | ✅ | ✅ |
| iPhone | Safari, not installed | — | ❌ (show install hint) | ✅ (in-app banner only) | ❌ | ❌ |
| Desktop | Chrome / Firefox | no | ✅ | n/a | ✅ | ✅ |
