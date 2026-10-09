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
    # Phase 4:  "snapshot:${CAM_GROUND_SNAPSHOT_URL}" (preferred)  or  "rtsp:${CAM_GROUND_RTSP_URL}"
    sample_every_s: 5
    slots_file: config/slots/cam-ground.json
    control_url: http://vision-occupancy:9000   # how the API reaches this worker (VPN address in T2)
    detector:
      runtime: ncnn                # ncnn | openvino | onnx | engine | hailo; per machine, see vision.md §11
      model: models/yolo11n-seg_ncnn_model
      imgsz: 1280
      conf: 0.35
      classes: [car, motorcycle, bus, truck]
      use_masks: true
    occupancy:
      method: detector             # detector | appearance (straight-down views, vision.md §2.1) | classifier | ensemble (§9)
      threshold: 0.30              # score above which a slot is "taken" (detector, appearance)
      mode: mask                   # mask | box_bottom (detector only)
      appearance:                  # appearance only; defaults shown, tuned in P1.4/P1.11
        inset: 0.12
        k_mad: 1.5
        min_delta_e: 12
        shadow_l_range: [0.35, 0.9]
        shadow_chroma_max: 2
        morph_frac: 0.06
        reference_empty: null      # optional path to an image of the empty lot
      classifier:                  # classifier / ensemble only (vision.md §9)
        model: models/slot_classifier.onnx   # from backend/scripts/train_slot_classifier.py
        threshold: 0.5             # on P(taken), or on the ensemble's mean
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
    control_url: http://vision-flow:9000
    detector:
      runtime: ncnn
      model: models/yolo11n_ncnn_model
      imgsz: 640
      conf: 0.4
      classes: [car, motorcycle, bus, truck]
      use_masks: false
    flow:
      min_track_frames: 5
      motion_min_area_px: 1500     # motion gate: px² at the line file's image_size

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
  - `reset` is only allowed on `flow` zones; `api.levels.filling < api.levels.plenty`
  - unknown keys are errors (catches typos such as `treshold`)
  - slot files: slot IDs unique, polygons ≥ 3 points and not self-intersecting (also `count_zones` and line-file `roi`)
- `${VAR}` substitution skips only lines whose first non-space character is `#`; a variable that is set but empty is substituted as empty.
- CLI tools run outside Docker (e.g. `parking analyze`) take `${VAR}` values from `cli_env(root)`: `deploy/.env.example` < `deploy/.env` < the process environment, so a dev checkout without `deploy/.env` still loads `lot.yaml` (the example holds placeholders, never secrets). Services in Docker use only their environment.
- `load_slots(path)` / `load_lines(path)` read §2/§3 files; `SlotFile.scaled(frame_w, frame_h)` rescales polygons; `LotConfig.zone_capacity(zone_id, slot_files)` gives the slot count for `slots` zones without a capacity.
- `Settings` (pydantic-settings) reads the environment and `deploy/.env` (relative to the working directory); empty values count as unset, every variable is optional for now: see §5.

### Source URI formats

| Scheme | Example | Behaviour |
|--------|---------|-----------|
| `file:` | `file:data/samples/ground-01.jpg` | The same image every time |
| `folder:` | `folder:data/replay/ground?interval=5&loop=true` | Images in name order, one per `interval` seconds. `loop=false` stops at the end. New files dropped into the folder are picked up |
| `snapshot:` | `snapshot:http://user:pass@10.0.20.11/cgi-bin/snapshot.jpg` | HTTP GET per sample (JPEG). **Preferred for occupancy** (no decoding between samples, full resolution) |
| `rtsp:` | `rtsp:rtsp://user:pass@10.0.20.12:554/sub` | Continuous stream on a reader thread that keeps only the latest frame. The occupancy fallback |
| `video:` | `video:data/recordings/cam-ramp-2026-11-02-0800.mp4?realtime=true&loop=false` | Plays a recording at its native fps like a live camera; `realtime=false` returns every frame as fast as possible (evaluation). For flow tests |

Relative paths resolve against the repo root. `folder:` reads `.jpg`/`.jpeg`/`.png`/`.bmp`/`.webp` files, re-scans the folder at the start of every pass, and only parses `interval` (default 5 s; the worker loop does the waiting: for `folder:` sources the occupancy worker samples every `interval`, which replaces the camera's `sample_every_s`). With `loop=false`, `read()` returns nothing after the last image and the source reports `exhausted`. A missing or unreadable image is a failed read (`connect_failed`), not an error. Unknown schemes and options are rejected; error messages never echo the URI (it may hold camera credentials).

**Camera sources** (`snapshot:`, `rtsp:`, P4.3). `snapshot:` takes the `user:pass@` out of the URL (percent-decoded, so encode `@ : /` in passwords) and answers the camera's 401 with **digest or basic**, whichever it asks for; credentials in the query string (some Reolink firmware: `?cmd=Snap&channel=0&user=…&password=…`) are passed as they are. Timeout 5 s; the body is decoded with `cv2.imdecode`; a non-200 or non-image answer is a failed read. `rtsp:` opens `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` with `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|timeout;5000000|fflags;nobuffer|flags;low_delay` (an operator's own value set before start-up wins; FFmpeg ≥ 5 calls the old `stimeout` `timeout`; the variable is set **only for the open**, and unset while `video:` files open, because the low-latency flags make OpenCV fail to open files) plus 5 s open/read timeouts; its reader thread starts on the first read, and `read()` returns the latest frame or nothing when it's **older than 5 s**. Both reconnect with **exponential backoff 1, 2, 4 … 60 s** (back to 1 s after a frame), log each attempt without the URL, and a failed or skipped read is `connect_failed` for health (vision.md §5).

**Low latency and recordings** (P5.2). The `rtsp:` reader thread reads as fast as the stream delivers and keeps **only the newest frame**: a frame no `read()` returned before the next one arrived is overwritten (counted in `dropped`), so a slow caller never works through a backlog. Its capture options add `fflags;nobuffer|flags;low_delay` (FFmpeg hands frames out without buffering). Frames from `rtsp:` and `video:` carry a `seq` number (the same `seq` = the same frame read again); both sources report `fps` (frames per second over the last 5 s). `video:` options: `realtime` (default `true`: the clock starts at the first read, `read()` waits until the next frame is due and skips frames the caller was too late for, counted in `dropped`) and `loop` (default `false`: at the end `read()` returns nothing and the source is `exhausted`). A frame's `ts` is the first read's wall time + position / native fps (also across loops), so tracking sees the video's own timing at any speed; a file without a usable fps counts as 10 fps; a missing or unreadable file is a failed read; URLs are rejected (`rtsp:` is for streams). Recordings come from `parking record` (architecture.md §6): FFmpeg **stream copy** of the first video stream (no audio, no re-encoding) over TCP into a **fragmented MP4** (`-movflags +frag_keyframe+empty_moov+default_base_moof`, so a recording cut short still plays), named `data/recordings/<camera>-<lot-local YYYY-MM-DD-HHMM>.mp4`.

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
Every slot not listed in `taken` or `unsure` is free. `unsure` slots are excluded from metrics. `load_labels(path)` reads it into `LabelFile` (unknown keys are errors; a slot can't be both `taken` and `unsure`).

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
| `API_INTERNAL_URL` | `http://api:8000` (same machine) or `http://<api-vpn-address>:8000` (T2) | workers |
| `API_HOST_PORT` | `8000` (bound to 127.0.0.1 only) | compose |
| `CAM_GROUND_SNAPSHOT_URL`, `CAM_GROUND_RTSP_URL` | `http://user:pass@10.0.20.11/...` | occupancy worker |
| `CAM_RAMP_RTSP_URL` | `rtsp://user:pass@10.0.20.12:554/sub` | flow worker |
| `CORS_ORIGINS` | dev: `https://iulian-redinciuc.github.io`; prod: the production frontend origin | API |
| `PUBLIC_APP_URL` | dev: `https://iulian-redinciuc.github.io/parking-app/`; prod: the production frontend URL | API (notification links) |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | from `parking push vapid-keys` | API |
| `VAPID_SUBJECT` | `mailto:you@example.com` | API |
| `ADMIN_PASSWORD_HASH` | from `parking admin hash-password` (prints the line in single quotes so Compose and dotenv keep the `$`s) | API |
| `ADMIN_TOKEN` | long random string | API (scripts / before Phase 7 login exists) |
| `DEBUG_CAPTURE` | `false` (save frames + observations to `data/debug/`, [vision.md §6.1](vision.md#61-debug-frame-capture-parkingworkersdebug_capturepy)) | workers |
| `DEBUG_CAPTURE_EVERY_MIN` | `10` (periodic capture interval; slot flips are captured too) | workers |
| `DEBUG_RETENTION_HOURS` | `24` (captures older than this are deleted, even with capture off) | workers |
| `TUNNEL_TOKEN` | from the Cloudflare dashboard | cloudflared |
| `LOG_LEVEL` | `INFO` | all |
| `PARKING_VERSION` | `latest` (dev) or `v0.x.y` (pinned in production) | compose (image tag) |
| `VISION_CPUS`, `FLOW_CPUS` | `1.0`, `1.5` | compose (worker CPU limits, per machine; [deployment.md §4](deployment.md#4-compose-deploy)) |

Frontend build-time variables (not secrets, because they end up in public JS; set per build: preview or production):

| Variable | Example |
|----------|---------|
| `VITE_BASE` | `/parking-app/` (GitHub Pages preview) or `/` (most other hosts) |
| `VITE_API_BASE` | `https://parking-api-dev.example.com`, `https://parking-api.example.com` or `mock` |
