# Generated sample images

AI-generated pictures of the car park with different cars in the spaces, used to test and demo the app.
They are **not real photos**, so they may be public. Real camera images never go in git (they stay in the
git-ignored `data/` folder).

- Same framing as the reference view, so the slot file `config/slots/cam-ground.json` lines up.
- Any name is fine, e.g. `gen-01.jpg` … `gen-12.jpg` (`.jpg`, `.jpeg` or `.png`).

## How they are used

`parking build-replay --camera cam-ground` (from `backend/`) puts them into the dev replay feed next to the
simulated frames, so the dev stack "sees" them as the ground camera. Spec:
[docs/design/vision.md §10](../../docs/design/vision.md#replay-feed-from-every-image-source-parkingvisionreplaypy-p213).

- An image is used only if it shows the same view as the reference (the command checks and says so per image)
  and is labelled in [`labels.json`](labels.json): the spaces with a car, by slot id.
- **After adding images:** run `uv run parking build-replay --camera cam-ground --review out/replay-review`,
  look at the overlays in `out/replay-review/`, add each new image's `taken` list to `labels.json`, run the
  command again, then `uv run parking evaluate --camera cam-ground --images samples/generated --labels samples/generated/labels.json`.

Checked 2026-10-11: all 12 images show the reference view and are in the feed; the scorer reads 204 of 204
spaces right on them. Three (`345324523452352345`, `3453245324523453425`, `4563456345634563456`) have repainted
pavement, so the automatic view check can't measure them; they were checked by eye on the overlay.
