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
