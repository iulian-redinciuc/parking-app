# Phase 7: Admin tools, history and stats

**Goal:** run and fix the system from your phone (no SSH), and give users useful history ("usually busy at 9").
**Needs hardware:** no.
**Specs used:** [api.md §2, §4](../design/api.md), [data-model.md §1, §4](../design/data-model.md), [frontend.md §2.3–2.4](../design/frontend.md), [security-privacy.md](../design/security-privacy.md).

---

## P7.1: Admin login
**Files:** `parking/api/routes/admin.py`, `parking/db/models.py` (`admin_session` + migration), CLI `admin hash-password`, `frontend/src/screens/admin/Login.tsx`, `frontend/src/api/client.ts`

**Steps**
1. `parking admin hash-password` prompts twice and prints an argon2 hash → `ADMIN_PASSWORD_HASH`.
2. `POST /api/admin/login`: verify with argon2 (constant time), rate limit 5/15 min/IP, create a session (store the **sha256 of the token**), return the token. `logout` revokes it.
3. The auth dependency accepts `ADMIN_TOKEN` **or** a valid session token ([api.md §4](../design/api.md#4-admin-endpoints)).
4. Frontend: the token goes in `sessionStorage`; the client adds `Authorization`; a 401 → back to the login screen.
5. Tests: good and bad password, rate limit, expired and revoked tokens.

**Done when:** you can log in on your phone and reach `#/admin`.

## P7.2: Camera health page and snapshots
**Files:** API `GET /api/admin/cameras`, `GET …/snapshot`; worker `snapshot` command (from P2.3); `frontend/src/screens/admin/Cameras.tsx`, `CameraDetail.tsx`

**Steps**
1. Snapshot round trip: the API publishes `cmd: snapshot` with a `request_id`, then waits up to 5 s for `snapshot/<request_id>` (an `asyncio.Future` keyed by id), and returns `image/jpeg` with `Cache-Control: no-store`.
2. The frontend fetches it with the auth header and displays it via a blob URL (an `<img src>` can't send headers).
3. The camera list shows state, issue, fps, last frame age and inference ms, auto-refreshing every 10 s.

**Done when:** you can see an annotated live snapshot of each camera on your phone.

## P7.3: Slot/line editor in admin
**Files:** `frontend/src/screens/admin/SlotEditor.tsx` (imports `tools/slot-editor/editor.js`), API `GET/PUT …/slots`, `GET/PUT …/lines`, `POST …/reference-frame`

**Steps**
1. Make the editor core importable by the frontend (a Vite alias to `../tools/slot-editor/editor.js`). Add touch support (tap to add a point, drag handles ≥ 24 px, two-finger zoom).
2. Load the current snapshot (not annotated) as the background, plus the current slots/lines JSON.
3. Save → `PUT` → the server validates with the same pydantic models, writes the file with a `.bak`, and sends `reload` to the worker.
4. A "Save reference frame" button for shift detection, offered after re-calibrating.

**Done when:** after nudging a camera (shift alert), you can fix the polygons from the phone, and the counts are right again.

## P7.4: Corrections UI and audit log
**Files:** `frontend/src/screens/admin/Zones.tsx`, API `GET /api/admin/corrections`

**Steps:** per flow zone: current value, an input for the real count, a note, and Save → `POST …/correct`. Below it: the last 50 corrections (who, when, old → new, note).

**Done when:** a correction from the phone updates everyone's app within 2 s and shows in the log.

## P7.5: Rollups and retention jobs
**Files:** `parking/db/repo.py`, `parking/api/app.py` (scheduler jobs), CLI `db aggregate`, `db prune`

**Steps:** implement the jobs in [data-model.md §4](../design/data-model.md#4-retention-and-rollups-apscheduler-jobs-in-the-api). The minute rollup is **time-weighted**: if free was 10 for 45 s and 12 for 15 s, the average is 10.5. Tests with synthetic timelines. `parking db aggregate --backfill` rebuilds from `zone_state`.

**Done when:** `zone_minute` / `zone_hour` fill up, and prune removes rows past the retention periods (test with a short retention).

## P7.6: History and forecast API
**Files:** `parking/api/routes/public.py`

**Steps**
1. `GET /api/history` per [api.md §2](../design/api.md#2-public-rest-endpoints): `minute` reads `zone_minute`; `hour` reads `zone_hour`; `day` aggregates `zone_hour`. Validate ranges (max 2000 points).
2. `GET /api/forecast`: the median of `free_avg` for the same weekday and hour over the last 8 weeks (needs ≥ 3 data points, otherwise 404 `not_enough_data`).
3. Cache responses for 60 s in memory.

**Done when:** integration tests on a seeded DB pass.

## P7.7: Stats screen
**Files:** `frontend/src/screens/StatsScreen.tsx`, `frontend/src/components/charts/*`

**Steps**
1. Lazy-load the route and Recharts.
2. "Today" line chart of free spaces, with a "typical" band (the median and IQR from `history?bucket=hour` for the same weekday over 8 weeks; computed client-side, or add a `typical` endpoint if it's slow).
3. Weekday × hour heatmap of average free.
4. The "Usually ~N free at HH:MM" card from `/api/forecast` (now + 30 min).
5. Charts must be readable in both themes, label their axes, and have a table fallback for screen readers.

**Done when:** with ≥ 2 weeks of data, the screen loads in < 1 s on a phone over 4G.

## P7.8: Admin alerts
**Steps**
1. Admin push subscriptions: a flag on `push_subscription` set by a logged-in admin from the Alerts screen ("Receive admin alerts").
2. Send `kind: admin_alert` when: a camera is `down` > 2 min, `shifted`, the data is stale > 5 min, or a flow counter clamps more than 3 times a day.
3. One alert per issue per hour, plus a "resolved" push.

**Done when:** unplugging a camera sends an alert within ~3 min and a resolved push after plugging it back in.

---

## Exit criteria
- [ ] Recalibrating a camera and correcting counts work from a phone, with no SSH
- [ ] Stats show ≥ 2 weeks of data; the forecast card appears
- [ ] Admin alerts fire for camera down and shift
- [ ] Admin endpoints reject requests with no token, an expired token or a wrong password (tests)
