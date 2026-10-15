"""The dev replay feed built from every image source (vision.md §10, P2.13).

Images stand in for the camera until a real one is connected: the simulated frames
(`parking simulate-feed`), the labelled real photos and the generated images in
`samples/generated/`. `parking build-replay` puts them into one folder for the `folder:`
source, with one labels file for all of it.

A still image is only usable if it shows the **same view** as the reference frame the slots
were drawn on. `check_view` decides, with the camera-shift code (`shift.py`):

- another shape (aspect ratio off by more than `ASPECT_TOL`) → `different`;
- displacement within `shift_max_px` → `aligned`, used as it is (resized to the reference);
- a larger shift, up to `MAX_SHIFT` of the image width → warped onto the reference framing and
  measured again: within the limit → `warped`, else `different`;
- too few matching features to measure (repainted pavement) → `unmeasured`: nothing says the
  view differs, nothing proves it is the same. Such an image is used only because a person
  labelled it on the slot overlay (`overlay`), and is listed as such.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from parking.config import ImageLabels, SlotFile
from parking.vision.shift import ShiftDetector, ShiftMeasure

ASPECT_TOL = 0.02  # width/height may differ this much (relative) from the reference's
MAX_SHIFT = 0.10  # of the reference width: beyond this it is another view, not a bumped one
JPEG_QUALITY = 92

USABLE = ("aligned", "warped", "unmeasured")


@dataclass(frozen=True)
class ViewCheck:
    status: str  # aligned | warped | unmeasured | different | unreadable
    reason: str
    measure: ShiftMeasure | None = None
    image: np.ndarray | None = None  # on the reference framing, when usable

    @property
    def usable(self) -> bool:
        return self.status in USABLE


def check_view(image: np.ndarray | None, detector: ShiftDetector) -> ViewCheck:
    """Does `image` show the reference view closely enough for its slot file?"""
    if image is None:
        return ViewCheck("unreadable", "can't read the image")
    rw, rh = detector.ref_size
    h, w = image.shape[:2]
    if abs((w / h) / (rw / rh) - 1) > ASPECT_TOL:
        return ViewCheck("different", f"{w}x{h} isn't the reference's shape ({rw}x{rh})")
    limit = detector.cfg.shift_max_px
    m = detector.measure(image)
    sized = detector.aligned(image, ShiftMeasure(None, 0, 0))
    if m.displacement_px is None:
        return ViewCheck(
            "unmeasured", f"only {m.inliers} matching features, checked by eye", m, sized
        )
    if m.displacement_px <= limit:
        return ViewCheck(
            "aligned", f"{m.displacement_px:.1f} px off ({m.inliers} features)", m, sized
        )
    if m.displacement_px > MAX_SHIFT * rw:
        return ViewCheck("different", f"{m.displacement_px:.0f} px off: another view", m)
    warped = detector.aligned(image, m)
    again = detector.measure(warped)
    if again.displacement_px is not None and again.displacement_px <= limit:
        reason = f"{m.displacement_px:.1f} px off, warped onto the reference"
        return ViewCheck("warped", f"{reason} ({again.displacement_px:.1f} px left)", m, warped)
    return ViewCheck("different", f"{m.displacement_px:.0f} px off and warping doesn't fix it", m)


def overlay(image: np.ndarray, slot_file: SlotFile) -> np.ndarray:
    """`image` (on the reference framing) with every slot outlined and named, for labelling."""
    out = image.copy()
    h, w = out.shape[:2]
    scale = np.array([w / slot_file.image_size[0], h / slot_file.image_size[1]])
    size = w / 1000
    for slot in slot_file.slots:
        pts = np.round(np.asarray(slot.polygon) * scale).astype(np.int32)
        cv2.polylines(out, [pts], True, (0, 255, 255), max(1, round(2.5 * size)))
        (tw, th), _ = cv2.getTextSize(slot.id, cv2.FONT_HERSHEY_SIMPLEX, size, round(2 * size))
        x, y = pts.mean(axis=0)
        org = (int(min(max(x - tw / 2, 0), w - tw)), int(y + th / 2))
        for colour, thick in (((0, 0, 0), 5), ((0, 255, 255), 2)):
            cv2.putText(
                out,
                slot.id,
                org,
                cv2.FONT_HERSHEY_SIMPLEX,
                size,
                colour,
                max(1, round(thick * size)),
            )
    return out


def up_and_down(names: Iterable[str], labels: Mapping[str, ImageLabels]) -> list[str]:
    """Stills ordered so the lot fills up and empties again (by number of taken slots)."""
    rising = sorted(names, key=lambda n: (len(labels[n].taken), n))
    return rising[::2] + rising[1::2][::-1]


def link(src: Path, dst: Path) -> None:
    """A hard link (the feed repeats frames; no second copy on disk), a copy where links fail."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def hold(image: np.ndarray, folder: Path, stem: str, frames: int) -> list[str]:
    """Write `image` once and repeat it `frames` times in the feed; the frame names."""
    first = folder / f"{stem}-01.jpg"
    if not cv2.imwrite(str(first), image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
        raise OSError(f"can't write {first}")
    names = [first.name]
    for i in range(2, frames + 1):
        name = f"{stem}-{i:02}.jpg"
        link(first, folder / name)
        names.append(name)
    return names


def clear(folder: Path, prefixes: Sequence[str]) -> None:
    """Remove the frames an earlier build left (only files with the feed's own prefixes)."""
    for old in folder.iterdir():
        if old.suffix == ".jpg" and old.name.startswith(tuple(prefixes)):
            old.unlink()
