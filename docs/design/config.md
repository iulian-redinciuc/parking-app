# Configuration

Three kinds of configuration:

| File | Committed? | Holds |
|------|-----------|-------|
| `config/lot.yaml` | ✅ yes | Zones, cameras, tuning parameters. **No secrets, no coordinates** |
| `config/slots/<camera>.json`, `config/lines/<camera>.json` | ✅ yes | Pixel polygons/lines drawn in the slot editor |
| `deploy/.env` (copy of `.env.example`) | ❌ never | Camera URLs, passwords, keys, lot location |

`config/lot.example.yaml` and `deploy/.env.example` are committed as templates.

---

## 1. `config/lot.yaml`

```yaml
version: 1

lot:
  id: main                         # used in logs and payloads
  name: "Parking"
  location:                        # values come from .env so they stay out of git
    lat: ${LOT_LAT}
    lon: ${LOT_LON}
  notify_radius_m: 500
  timezone: ${TZ}                  # e.g. Europe/Bucharest; used for schedules and stats buckets

zones:
  - id: ground
    name: { en: "Ground" }
    method: slots                  # slots | count | flow
    capacity: null                 # null = number of slots in this zone (slots method only)
  - id: underground
    name: { en: "Underground" }
    method: flow
    capacity: 60
    reset:                         # optional, flow zones only
      enabled: false
      cron: "0 3 * * *"            # local time
      value: 0

cameras:
  - id: cam-ground
    role: occupancy                # occupancy | flow
    zones: [ground]                # zones this camera reports on
    source: "file:data/samples/ground-01.jpg"
    # Phase 2:  "folder:data/replay/ground?interval=5&loop=true"
    # Phase 4:  "snapshot:${CAM_GROUND_SNAPSHOT_URL}"  or  "rtsp:${CAM_GROUND_RTSP_URL}"
    sample_every_s: 5
    slots_file: config/slots/cam-ground.json
    detector:
      model: models/yolo11n-seg_ncnn_model
      imgsz: 1280
      conf: 0.35
      classes: [car, motorcycle, bus, truck]
      use_masks: true
    occupancy:
      threshold: 0.30              # overlap ratio above which a slot is "taken"
      mode: mask                   # mask | box_bottom
    smoothing:
      consistent_readings: 3
    health:
      black_mean_max: 12
      frozen_diff_max: 0.5
      frozen_frames: 6
      blur_laplacian_min: 40
      shift_check_every_s: 300
      shift_max_px: 8

  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "rtsp:${CAM_RAMP_RTSP_URL}"
    fps: 10
    lines_file: config/lines/cam-ramp.json
    detector:
      model: models/yolo11n_ncnn_model
      imgsz: 640
      conf: 0.4
      classes: [car, motorcycle, bus, truck]
      use_masks: false
    flow:
      min_track_frames: 5
      motion_min_area_px: 1500

api:
  stale_after_s: 60
  sse_ping_s: 15
  trend_window_min: 15
  levels:                          # free / capacity thresholds (see api.md#levels)
    plenty: 0.20
    filling: 0.05
```

### Loader rules (`parking/config.py`)
- `${VAR}` is replaced from the environment **before** YAML parsing. A missing variable is an error, unless it's used in a commented-out line.
- Validation (pydantic), with the error naming the field:
  - zone and camera IDs are unique and match `^[a-z0-9-]+$`
  - every `cameras[].zones[]` exists in `zones`
  - `slots` zones have at least one occupancy camera, and `flow` zones exactly one flow camera
  - `capacity` is required for `count` and `flow` zones; for `slots` zones it defaults to the number of slots
  - occupancy cameras need `slots_file`, flow cameras need `lines_file` (the file may be missing in Phase 1 before slots are drawn; commands that need it fail with a clear message)
  - `0 < threshold < 1`, `consistent_readings ≥ 1`
- `Settings` (pydantic-settings) reads `.env`: see §5.

### Source URI formats

| Scheme | Example | Behaviour |
|--------|---------|-----------|
| `file:` | `file:data/samples/ground-01.jpg` | The same image every time |
| `folder:` | `folder:data/replay/ground?interval=5&loop=true` | Images in name order, one per `interval` seconds. `loop=false` stops at the end. New files dropped into the folder are picked up |
| `snapshot:` | `snapshot:http://user:pass@10.0.20.11/cgi-bin/snapshot.jpg` | HTTP GET per sample (JPEG) |
| `rtsp:` | `rtsp:rtsp://user:pass@10.0.20.12:554/sub` | Continuous stream on a reader thread that keeps only the latest frame |
| `video:` | `video:data/recordings/ramp-2026-11-02.mp4` | Plays a file at its native fps (for flow tests) |

---

## 2. Slot file: `config/slots/<camera>.json`

Made by the slot editor (`tools/slot-editor`) or `parking bootstrap-slots`.

```json
{
  "version": 1,
  "camera_id": "cam-ground",
  "image_size": [2560, 1440],
  "reference_image": "data/reference/cam-ground.jpg",
  "slots": [
    { "id": "G01", "zone": "ground", "polygon": [[412, 980], [598, 975], [640, 1180], [430, 1190]], "type": "standard" },
    { "id": "G02", "zone": "ground", "polygon": [[598, 975], [781, 970], [842, 1172], [640, 1180]], "type": "accessible" }
  ],
  "count_zones": [
    { "zone": "ground", "polygon": [[0, 600], [2560, 600], [2560, 1440], [0, 1440]] }
  ]
}
```

- `polygon`: at least 3 `[x, y]` points in pixels of `image_size`, clockwise or anticlockwise, non-self-intersecting.
- `id`: unique per lot. Convention: zone letter + two digits (`G01`, `U17`).
- `type`: `standard | accessible | ev | motorcycle | reserved` (Phase 9 uses it; until then informational).
- `count_zones`: only for `count`-method zones (vehicles counted inside the polygon).
- `reference_image` is git-ignored data; only the path is committed.

## 3. Line file: `config/lines/<camera>.json`

```json
{
  "version": 1,
  "camera_id": "cam-ramp",
  "image_size": [640, 360],
  "roi": [[60, 120], [600, 120], [600, 360], [60, 360]],
  "line_a": [[100, 220], [560, 220]],
  "line_b": [[100, 270], [560, 270]],
  "in_direction": "a_to_b"
}
```

- A car counts as **IN** when it crosses `line_a` then `line_b` (if `in_direction` is `a_to_b`); the reverse order is **OUT**.
- `roi` (optional): the detector and motion gate only look inside this polygon.

## 4. Ground-truth labels

### Occupancy: `data/labels/<camera>.json` (git-ignored, because it's tied to real images)
```json
{
  "version": 1,
  "camera_id": "cam-ground",
  "images": {
    "ground-01.jpg": { "conditions": ["day", "dry"], "taken": ["G01", "G04"], "unsure": ["G09"] },
    "ground-02.jpg": { "conditions": ["night"], "taken": ["G01"], "unsure": [] }
  }
}
```
Every slot not listed in `taken` or `unsure` is free. `unsure` slots are excluded from metrics.

### Flow: `data/labels/<clip>.csv`
```csv
video_time_s,direction,note
12.4,in,
57.9,out,van
```

## 5. Environment variables (`deploy/.env`)

| Variable | Example | Used by |
|----------|---------|---------|
| `TZ` | `Europe/Bucharest` | all |
| `PARKING_CONFIG` | `/app/config/lot.yaml` | all |
| `LOT_LAT`, `LOT_LON` | `51.5007`, `-0.1246` (example) | API (lot location) |
| `PARKING_DB_URL` | `sqlite:////app/data/db/parking.sqlite` | API |
| `WORKER_TOKEN` | `openssl rand -hex 32` | API, workers (internal endpoints) |
| `API_INTERNAL_URL` | `http://api:8000` | workers |
| `API_HOST_PORT` | `8000` (bound to 127.0.0.1 only) | compose |
| `CAM_GROUND_SNAPSHOT_URL`, `CAM_GROUND_RTSP_URL` | `http://user:pass@10.0.20.11/...` | occupancy worker |
| `CAM_RAMP_RTSP_URL` | `rtsp://user:pass@10.0.20.12:554/sub` | flow worker |
| `CORS_ORIGINS` | `https://iulian-redinciuc.github.io` | API |
| `PUBLIC_APP_URL` | `https://iulian-redinciuc.github.io/parking-app/` | API (notification links) |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | from `parking push vapid-keys` | API |
| `VAPID_SUBJECT` | `mailto:you@example.com` | API |
| `ADMIN_PASSWORD_HASH` | from `parking admin hash-password` | API |
| `ADMIN_TOKEN` | long random string | API (scripts / before Phase 7 login exists) |
| `DEBUG_CAPTURE` | `false` | workers |
| `DEBUG_RETENTION_HOURS` | `24` | workers |
| `TUNNEL_TOKEN` | from the Cloudflare dashboard | cloudflared |
| `LOG_LEVEL` | `INFO` | all |

Frontend build-time variables (GitHub repo **variables**, not secrets, because they end up in public JS):

| Variable | Example |
|----------|---------|
| `VITE_API_BASE` | `https://parking-api.example.com` or `mock` |
