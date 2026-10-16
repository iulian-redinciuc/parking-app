# Phase 9: Optional extras

Pick in any order once Phase 8 is done. Each item is a small project; write a short design note in `docs/design/` before starting it. Everything stays **web only** (no native app).

## P9.1: Ground-level slot map
**Why:** show *which* spaces are free, not just how many.
**Design note:** [slot-map.md](../design/slot-map.md).
**Files:** `backend/parking/slot_map.py`, `parking slot-map` (`cli.py`), `GET /api/maps/{zone}` (`api/routes/public.py`), zone `map` (`config.py`, `config/lot.yaml`), `config/maps/ground.svg`; `frontend/src/lib/slotMap.ts`, `src/components/SlotMap.tsx` (on `ZoneCard`), `getSlotMap` + `mockSlotMap`.
**Steps**
1. `config/maps/ground.svg`: a simple schematic with one `<rect id="G01">` per slot (drawn once, by hand or generated from the slot polygons with a top-down projection). Generated with `uv run parking slot-map --camera cam-ground` (straight-down cameras; `--check` compares an existing or hand-drawn map with the slot file).
2. The zone names it in lot.yaml (`map: config/maps/ground.svg`) and the API serves it at `GET /api/maps/ground`.
3. The frontend colours each rect from `LotStatus.zones[].slots`. Tapping one shows the slot id.

**Done when:** the map matches reality at a glance. Checked on the dev Pi against the simulated feed (slot-map.md §5); to look at it yourself: the Live screen → *Show map* on the Ground card, next to the frame the camera sees (Admin → the camera's snapshot).

## P9.2: Special spaces
**Why:** "2 accessible spaces free", "EV charger free".
**Design note:** [special-spaces.md](../design/special-spaces.md).
**Files:** `backend/parking/messages.py` (`TypeCount`, `ZoneStatus.by_type`), `core/fusion.py` (`_by_type`), `push/payload.py`, `push/rules.py` (`special_flipped`), `push/dispatch.py`, `api/routes/push.py` (`Prefs.space_types`); `frontend/src/components/ZoneCard.tsx`, `src/lib/status.ts`, `src/lib/push.ts`, `src/screens/NotificationsScreen.tsx`, `src/api/mock.ts`.
**Steps**
1. Give the special spaces their `type` in the slot file (admin slot editor → select a space → *Type*): `accessible`, `ev`, `motorcycle` or `reserved`.
2. The API adds `by_type` counts per `slots` zone to `LotStatus` (`{"accessible": {"capacity": 2, "free": 1}}`).
3. The zone card shows a small chip per type the zone has.
4. Alert preference per type (`prefs.space_types`, Alerts → *Special spaces*): the followed types are named in every push, and an on-my-way window also pushes when one runs out or comes back.

**Done when:** a space marked accessible in the slot file shows on its zone's card as `Accessible n / m`, and `n` follows that space being taken and freed. Checked on the dev Pi against the simulated feed (special-spaces.md §5). The committed slot file marks no special spaces yet: mark the real ones once the real lot's layout is drawn (P4.5).

## P9.3: Barrier / induction-loop integration
**Why:** near-perfect entry/exit counts where a barrier exists.
**Design note:** [barrier.md](../design/barrier.md).
**Files:** `backend/parking/workers/contacts.py` (GPIO and replay sources, `pulse` / `pair` decoders), `workers/barrier_worker.py`, `parking worker barrier` (`cli.py`), `config.py` (`role: barrier`, `barrier:`, zone `counted_by`), `messages.py` (`FlowEventMsg.source`), `core/fusion.py`, `api/ingest.py`, `db/models.py` + migration `0007` (`flow_event.source`, `counted`), `push/admin_alerts.py` (`flow_mismatch`); `deploy/docker-compose.barrier.yml` and the `barrier` service; `frontend/src/api/client.ts`, `en.json` (the role and the alert).
**Steps**
1. A small worker reads the barrier controller's dry contacts on GPIO inputs of the vision box (`gpio:` source; `contacts:` replays a file for tests) and sends the same `POST /internal/flow-events` with `source: "barrier"`. A controller's network API is not built: none is known ([barrier.md §6](../design/barrier.md#6-not-built-and-why)).
2. In lot.yaml the barrier is a `cameras` entry with `role: barrier` on a `flow` zone, alone or next to the flow camera.
3. With both, one counts (the barrier unless `counted_by` says otherwise) and the admin alert `flow_mismatch` fires when their net counts differ by more than 2 cars.

**Done when:** cars through the barrier move the zone's count, and with a flow camera on the same zone a disagreement raises the alert. Checked on the dev Pi with a replayed contact file, camera events sent by hand and two unused GPIO pins (barrier.md §7). **On a real barrier** (when one exists): wire it per [hardware.md §4.7](../design/hardware.md#47-barrier-contacts-p93), then

```bash
cd deploy && echo "GPIO_GID=$(getent group gpio | cut -d: -f3)" >> .env
docker compose -f docker-compose.site.yml -f docker-compose.barrier.yml --profile barrier up -d
LOG_LEVEL=DEBUG docker compose -f docker-compose.site.yml -f docker-compose.barrier.yml --profile barrier \
  run --rm --no-deps barrier barrier --camera barrier-ramp --print     # drive through once each way: one line per edge and per car
```

## P9.4: Fine-tuned detector / licence swap
**Why:** better accuracy on your specific view, or avoiding AGPL obligations.
**Design note:** [detector-training.md](../design/detector-training.md).
**Files:** `backend/parking/vision/yolo_dataset.py` + `parking export-yolo` (`cli.py`), `vision/yolox.py` (`YoloxDetector`), `vision/detector.py` (`build_detector`), `config.py` (`detector.type`), `vision/validation.py` (`held_out`, shared with the slot classifier).
**Steps**
1. Export the labelled validation frames (P4.8) to YOLO format: `uv run parking export-yolo --camera cam-ground --review` → `data/yolo/cam-ground/` (the slot polygons give rough boxes: look through `review/`, correct the label files, then `--coco` for YOLOX). 20% of the frames are held out.
2. On a desktop GPU or Colab, fine-tune YOLO11n ([§3](../design/detector-training.md#3-route-a-fine-tune-yolo11n)), or train **YOLOX-nano** (Apache-2.0, [§4](../design/detector-training.md#4-route-b-yolox-nano)), which runs through `YoloxDetector` (`detector.type: yolox`).
3. Compare with `parking evaluate` on the held-out frames ([§5](../design/detector-training.md#5-comparing)) and switch the camera's `detector` in lot.yaml only if the new model meets the target and beats the current method.

**Done when:** a model trained on the real camera's frames is evaluated on the held-out set against the current method, the table is in PROGRESS.md → Metrics and the better one is configured. **Built and checked on the dev Pi without real frames** (detector-training.md §7): the export, a short CPU fine-tune on simulated frames to prove the format, and `YoloxDetector` on the official YOLOX-nano. **Waiting for:** P4.8's labelled frames and a machine with a GPU.

**PoC check (2026-10-16):** on images on the dev Pi, with P4.8's 200 PoC validation frames (lit copies of the sample scene: day, dusk, IR night, rain) as the camera's frames. `parking export-yolo --camera cam-ground --review --out out/yolo/cam-ground`: 167 train + 33 held-out frames, 1518 boxes, the review pictures show each box on its car. YOLO11n fine-tuned on the Pi's CPU (`imgsz=640`, `flipud`/`fliplr` 0.5, from `yolo11n.pt`; stopped by the 20-min limit during epoch 5, epoch 4's `best.pt` used: mAP50 0.988, mAP50-95 0.834), exported to NCNN and compared with `parking evaluate` on the 33 held-out frames (561 slots): **appearance (configured today) 99.1%, count error ≤ 1 on 97.0%, target met**; fine-tuned YOLO11n `box_bottom` at `conf: 0.25` 74.0% at best (the short training leaves its scores low), at `conf: 0.05` **98.2% at threshold 0.10, count error ≤ 1 on 93.9%, target missed**; the official YOLOX-nano through `YoloxDetector` 53.5% (COCO doesn't see cars from above). The fine-tuned model doesn't beat the current method, so `cam-ground` stays on `appearance` and lot.yaml is unchanged; the weights were deleted. Lit copies of one photo, a 4-epoch CPU run and no YOLOX training: nothing about the real lot ([go-live.md §3](../go-live.md#p94-fine-tuned-detector--licence-swap)).

## P9.5: Multiple lots
**Why:** reuse for other locations.
**Design note:** [multi-lot.md](../design/multi-lot.md).
**Files:** `backend/parking/config.py` (`load_lots`, `lot_of_camera`, `total_key`), `api/app.py` + `api/deps.py` (a `Runtime` per lot, `LotDep` / `CameraLotDep` / `ZoneLotDep`), `api/routes/public.py` (`/api/lots`, `/api/lots/{lot}/…`), `routes/internal.py`, `routes/push.py`, `routes/admin.py`, `api/jobs.py` + `db/rollups.py` (a total per lot), `messages.py` (`lot` in worker payloads), `workers/*`, `push/dispatch.py` (`of_lots`), `db/models.py` + migration `0008` (`push_subscription.lot_id`); `frontend/src/api/lots.ts`, `src/components/LotPicker.tsx` (in `Header`), `src/api/client.ts` (`getLots`, lot paths), `src/lib/push.ts`.
**Steps**
1. `lots: [...]` in lot.yaml: each entry a lot with its own `zones` and `cameras`; the first is the default lot. Zone and camera ids are unique across the file. The single-lot form keeps working.
2. `GET /api/lots` and `/api/lots/<id>/status` (`/stream`, `/maps/…`, `/history`, `/forecast`); the paths without a lot answer for the default lot.
3. Worker payloads carry `lot`; the API routes them by `camera_id` and rejects a wrong `lot`.
4. A lot picker in the app's header when there is more than one lot.
5. A push subscription follows one lot (`lot` in the subscribe / PATCH bodies).

**Done when:** with two lots in lot.yaml each has its own live count in the app, chosen with the picker, and a single-lot installation is unchanged. Checked on the dev Pi with a throwaway two-lot API and two workers on the simulated feed (multi-lot.md §8). To add a real second location: [runbook](../runbook.md#add-a-language--zone--camera--lot).

## P9.6: Smarter forecast
**Why:** "you'll probably find a space if you arrive at 9:10".
**Design note:** [forecast.md](../design/forecast.md).
**Files:** `backend/parking/forecast/` (`gbm.py`, `days.py`, `weather.py`, `model.py`), `config.py` (`ForecastCfg`), `db/models.py` + migration `0009` (`weather_hour`, `forecast_model`), `api/jobs.py` (job `forecast`), `api/routes/public.py` (`/api/forecast`), `cli.py` (`parking forecast train` / `status`); `frontend/src/screens/StatsScreen.tsx`, `src/api/types.ts`, `validate.ts`.
**Steps**
1. Features per hour: lot-local hour, weekday, month; public holiday / the day before / the day after (the `holidays` package, `forecast.holidays` in lot.yaml); the 8-week median and its spread; temperature and rain from Open-Meteo (`forecast.weather`, off by default).
2. A small gradient-boosting model per zone (numpy, no new ML dependency) that predicts how far the hour ends up from the median; trained nightly at 03:30 on `zone_hour`.
3. The check: trained on the hours before the last 4 weeks, compared with the median on those 4 weeks; used only if its mean absolute error is at least 3% lower. The result is in `forecast_model` either way.
4. `/api/forecast` answers from the model while it is active (`method: "model"`), else the P7.6 median; the Stats card says "Probably ~N free" instead of "Usually ~N free".

**Done when:** `parking forecast status` shows, per zone, the model's and the median's error on the held-out 4 weeks and which one is used, and `/api/forecast` follows it. **Checked on the dev Pi on synthetic history only** (forecast.md §8): the model is used when holidays and rain move the lot and is not used when every week is alike. On the real lot the nightly check decides by itself once there are about 9 weeks of history; nothing to do by hand except setting `forecast.holidays` to the lot's country.
