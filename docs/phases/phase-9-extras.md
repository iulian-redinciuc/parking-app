# Phase 9: Optional extras

Pick in any order once Phase 8 is done. Each item is a small project; write a short design note in `docs/design/` before starting it. Everything stays **web only** (no native app).

## P9.1: Ground-level slot map
**Why:** show *which* spaces are free, not just how many.
**Outline**
1. `config/maps/ground.svg`: a simple schematic with one `<rect id="G01">` per slot (drawn once, by hand or generated from the slot polygons with a top-down projection).
2. The frontend colours each rect from `LotStatus.zones[].slots`. Tapping one shows the slot id.
**Done when:** the map matches reality at a glance.

## P9.2: Special spaces
**Why:** "2 accessible spaces free", "EV charger free".
**Outline:** use the slot `type` field; add `by_type` counts per zone to `LotStatus` (`{"accessible": {"capacity": 2, "free": 1}}`); show small chips on the zone card; optional alert preferences per type.

## P9.3: Barrier / induction-loop integration
**Why:** near-perfect entry/exit counts where a barrier exists.
**Outline:** a small worker reading the barrier controller (dry contact via a GPIO input on the vision box, or the controller's API) → the same `POST /internal/flow-events` with `source: "barrier"`. Use it alone or to cross-check the camera (alert on disagreement).

## P9.4: Fine-tuned detector / licence swap
**Why:** better accuracy on your specific view, or avoiding AGPL obligations.
**Outline:** export the labelled validation frames (P4.8) to YOLO format (the slot polygons give rough boxes; review them). Fine-tune YOLO11n on a desktop GPU or Colab. Or train **YOLOX-nano** (Apache-2.0) and add a `YoloxDetector`. Compare with `parking evaluate` on the held-out set.

## P9.5: Multiple lots
**Why:** reuse for other locations.
**Outline:** `lots: [...]` in config; `lot` in every API path (`/api/lots/<id>/status`); a lot picker in the UI; per-lot subscriptions. Payloads already carry `camera_id`, so add `lot` to them.

## P9.6: Smarter forecast
**Why:** "you'll probably find a space if you arrive at 9:10".
**Outline:** add calendar features (weekday, hour, holidays, weather from a free API) → a small gradient-boosting model trained nightly on `zone_hour`. Show it only if it beats the median baseline (P7.6) on a held-out month.
