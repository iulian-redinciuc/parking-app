# Flow tally

A standalone web page for counting the cars that cross the flow camera's lines in a recorded
clip, by hand. The result is the ground truth for `parking evaluate-flow` (P5.9): a CSV in the
format of [docs/design/config.md §4](../../docs/design/config.md#flow-datalabelsclipcsv).
Plain HTML + an ES module with no build step and no dependencies.

## Open it

Browsers don't load ES modules from `file://`, so serve the repo over http (localhost only):

```bash
python3 -m http.server 8765 --bind 127.0.0.1     # from the repo root
```

Then open http://127.0.0.1:8765/tools/flow-tally/ and _Open video_ (e.g. a
`data/recordings/*.mp4` from `parking record`). The clip never leaves the browser.

## Use it

Play the clip and press a key **when the car has crossed the second line** on its way (line B
going in, line A going out: the moment the counter counts it):

| Key                       | What it does                                |
| ------------------------- | ------------------------------------------- |
| <kbd>I</kbd>              | a car went **in** at the current time       |
| <kbd>O</kbd>              | a car went **out**                          |
| <kbd>U</kbd>              | undo the last key press                     |
| <kbd>Space</kbd>          | play / pause                                |
| <kbd>1</kbd>–<kbd>4</kbd> | speed 1×–4×                                 |
| <kbd>←</kbd> <kbd>→</kbd> | 2 s back / forward (<kbd>Shift</kbd>: 10 s) |

- The side panel lists the events by time with IN / OUT / NET counts. Click a time to jump to
  2 s before it, type a note (`van`, `queued at the barrier`, `reversed`), or × to delete one.
- _Export CSV_ downloads `<clip name>.csv`: save it as `data/labels/<clip name>.csv` (git-ignored),
  the path `evaluate-flow` looks for by default.
- The events are kept in the browser (localStorage, per clip file name), so a reload or opening
  the same clip again carries on where you stopped. _Clear_ empties the list.
- _Import CSV_ loads a labels file to continue it, or `evaluate-flow`'s
  `out/eval/flow-<clip>-pred.csv` to step through what the pipeline counted.
- Press at the second line, not between the lines: a car queueing at 5 km/h takes ~3.5 s from one
  line to the other, more than the ±2 s window.
- Keypresses lag the crossing a little; `evaluate-flow` matches within ±2 s, so tally at 1–2×
  in busy stretches and fast-forward the quiet ones.

## Code

`tally.js` holds the pure parts (event list with undo, CSV read/write with the same checks as
`parking evaluate-flow`, time formatting); `index.html` wires them to a `<video>`. Checks, from
the repo root after `npm ci` in `frontend/`:

```bash
node --test tools/flow-tally/tally.test.js
frontend/node_modules/.bin/eslint tools/flow-tally
frontend/node_modules/.bin/prettier --config frontend/.prettierrc.json --check tools/flow-tally
```
