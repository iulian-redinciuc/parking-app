# Slot editor

A standalone web page for drawing parking spaces (`config/slots/<camera>.json`), flow lines
(`config/lines/<camera>.json`) and ground-truth labels (`data/labels/<camera>.json`) on a camera
image. The file formats are in [docs/design/config.md §2–4](../../docs/design/config.md#2-slot-file-configslotscamerajson).
Plain HTML + an ES module with no build step and no dependencies.

## Open it

Browsers don't load ES modules from `file://`, so serve the repo over http (localhost only):

```bash
python3 -m http.server 8765 --bind 127.0.0.1     # from the repo root
```

Then open http://127.0.0.1:8765/tools/slot-editor/. Optional query parameters load files from the
same server, e.g.
`?image=/data/reference/cam-ground.jpg&slots=/config/slots/cam-ground.json&mode=label`
(`image`, `slots`, `lines`, `labels`, `mode`).

## Use it

| Mode      | What you do                                                                                                                                                                                                                                                                                                                                                     | Export → save as                                            |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| **Slots** | _Open image_ (the reference frame), click corners, then click the first point or press <kbd>Enter</kbd> to close a space. Clicks near an existing corner snap to it, so neighbouring spaces share edges. Ids are auto-named from the zone dropdown (`G01`, `G02`, … / `U01`). Click a space to select it: drag its corners, edit id/zone/type in the side panel | _Export slots_ → `config/slots/<camera>.json`               |
| **Lines** | Pick `line_a`, `line_b` (2 clicks each) or `roi` (a polygon), and `in_direction`                                                                                                                                                                                                                                                                                | _Export lines_ → `config/lines/<camera>.json`               |
| **Label** | _Import slots_, _Open image(s)_ (several at once), optionally _Import labels_ to continue a file. Click spaces: free → taken → unsure. Set the conditions (`day, dry`, `night`, …); _Mark labelled_ records an image where every space is free                                                                                                                  | _Export labels_ → `data/labels/<camera>.json` (git-ignored) |

Keys: <kbd>Esc</kbd> cancel / deselect · <kbd>Enter</kbd> close polygon · <kbd>Delete</kbd>
remove space · <kbd>D</kbd> duplicate right (shifted by its own width) · <kbd>Shift</kbd>+<kbd>D</kbd>
duplicate down · <kbd>←</kbd> <kbd>→</kbd> previous / next image · <kbd>F</kbd> fit ·
<kbd>+</kbd> <kbd>-</kbd> zoom. Mouse wheel or pinch zooms; drag pans.

- `image_size` is the natural size of the loaded image. Importing a file drawn on a different
  size scales it to the image (the status bar says so).
- _Export slots_ refuses files that `load_slots()` would reject (duplicate ids, < 3 points,
  self-crossing polygons). Importing and exporting an unchanged file gives the same text back.
- `count_zones` in an imported slot file are kept and shown dashed, but not edited here.
- Images never leave the browser; only the JSON you export is written.

## Code

`editor.js` exports `createEditor(canvas, opts)` and the pure helpers (geometry, id naming, file
formats, labels). The admin's editor (`#/admin/cameras/<id>/edit`, P7.3) imports it through the
Vite alias `@slot-editor`, so a change here ships in the frontend too. Touch is built in: 24 px
hit areas and bigger handles for fingers, two-finger pan/zoom, and `setMoveAll(true)` to drag
every shape at once (`translatePolys`); `undoPoint()` and `destroy()` are for the admin page. Checks, from the repo root after `npm ci` in `frontend/`:

```bash
node --test tools/slot-editor/editor.test.js
frontend/node_modules/.bin/eslint tools/slot-editor
frontend/node_modules/.bin/prettier --config frontend/.prettierrc.json --check tools/slot-editor
```
