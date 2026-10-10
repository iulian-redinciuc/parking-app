"""The slot map (docs/design/slot-map.md): a schematic SVG with one shape per parking space.

`build_slot_map` draws one from a slot file's polygons, for cameras that look straight down
(the picture is already a top-down view; an angled view needs a hand-drawn map).
`map_slot_ids` reads the slot ids back from any map, generated or drawn by hand.
"""

import math
import statistics
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from parking.config import Slot, SlotFile
from parking.geometry import to_polygon

SVG_NS = "http://www.w3.org/2000/svg"
# elements that can be a slot (what the frontend draws, frontend/src/lib/slotMap.ts)
SHAPES = ("rect", "polygon", "path", "circle", "ellipse")
# the API refuses to serve a bigger file
MAX_BYTES = 200_000

# the longer side of the drawing, in SVG units, and the empty border around it
MAP_SIZE = 1000.0
MARGIN = 20.0


class SlotMapError(ValueError):
    pass


def _rectangle(slot: Slot) -> tuple[float, float, float, float, float]:
    """`(cx, cy, width, height, angle°)` of the slot's smallest enclosing rectangle; the angle
    (how far `width` is turned from the x axis, clockwise on screen) is within ±45°."""
    polygon = to_polygon(slot.polygon)
    corners = list(polygon.minimum_rotated_rectangle.exterior.coords)[:4]
    (x0, y0), (x1, y1), (x2, y2) = corners[:3]
    width, height = math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x1, y2 - y1)
    angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
    # the same rectangle has four descriptions (90° apart): take the one closest to upright
    angle = (angle + 45) % 180 - 45
    if angle > 45:
        angle -= 90
        width, height = height, width
    cx, cy = (x0 + x2) / 2, (y0 + y2) / 2
    return cx, cy, width, height, angle


def build_slot_map(
    slot_file: SlotFile, zone_id: str, *, snap_deg: float = 10.0, gap: float = 0.06
) -> str:
    """An SVG with one `<rect id="<slot id>">` per slot of `zone_id`, laid out as in the picture.

    Rectangles turned by less than `snap_deg` are drawn upright (a tidy schematic; 0 keeps every
    angle). `gap` is the space left around each one, as a share of the typical slot's short side.
    """
    slots = [s for s in slot_file.slots if s.zone == zone_id]
    if not slots:
        raise SlotMapError(f"no slots of zone '{zone_id}' in the slot file")
    rects = []
    for slot in slots:
        cx, cy, w, h, angle = _rectangle(slot)
        if abs(angle) <= snap_deg:
            angle = 0.0
        rects.append((slot.id, cx, cy, w, h, angle))

    inset = gap * statistics.median(min(w, h) for _, _, _, w, h, _ in rects)
    xs, ys = [], []
    for _, cx, cy, w, h, angle in rects:
        # the turned rectangle's own bounding box
        cos, sin = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))
        half_w, half_h = (w * cos + h * sin) / 2, (w * sin + h * cos) / 2
        xs += [cx - half_w, cx + half_w]
        ys += [cy - half_h, cy + half_h]
    left, top = min(xs), min(ys)
    scale = (MAP_SIZE - 2 * MARGIN) / max(max(xs) - left, max(ys) - top)
    view_w = (max(xs) - left) * scale + 2 * MARGIN
    view_h = (max(ys) - top) * scale + 2 * MARGIN

    def n(value: float) -> str:
        return f"{value:.1f}".rstrip("0").rstrip(".")

    lines = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {n(view_w)} {n(view_h)}" '
        f"data-zone={quoteattr(zone_id)} data-camera={quoteattr(slot_file.camera_id)}>",
        "  <!-- Slot map (docs/design/slot-map.md): one shape per space, its id = the slot id. "
        "Made by `parking slot-map`; the app colours the shapes, so no styles here. -->",
    ]
    for slot_id, cx, cy, w, h, angle in rects:
        w, h = max(w - inset, 1.0) * scale, max(h - inset, 1.0) * scale
        cx, cy = (cx - left) * scale + MARGIN, (cy - top) * scale + MARGIN
        turn = f' transform="rotate({n(angle)} {n(cx)} {n(cy)})"' if angle else ""
        lines.append(
            f'  <rect id={quoteattr(slot_id)} x="{n(cx - w / 2)}" y="{n(cy - h / 2)}" '
            f'width="{n(w)}" height="{n(h)}" rx="{n(min(w, h) * 0.08)}"{turn}/>'
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def map_slot_ids(svg: str) -> list[str]:
    """The ids of the shapes in a map, in document order (other elements' ids don't count)."""
    try:
        root = ElementTree.fromstring(svg)
    except ElementTree.ParseError as e:
        raise SlotMapError(f"not valid XML: {e}") from e
    if root.tag != f"{{{SVG_NS}}}svg":
        raise SlotMapError(f'the root element must be <svg xmlns="{SVG_NS}">')
    if not root.get("viewBox"):
        raise SlotMapError("the <svg> element needs a viewBox")
    shapes = {f"{{{SVG_NS}}}{name}" for name in SHAPES}
    return [el.get("id") for el in root.iter() if el.tag in shapes and el.get("id")]


def compare_slot_map(svg: str, slot_file: SlotFile, zone_id: str) -> tuple[list[str], list[str]]:
    """`(missing, unknown)`: the zone's slots without a shape, and shape ids that are no slot."""
    on_map = map_slot_ids(svg)
    wanted = [s.id for s in slot_file.slots if s.zone == zone_id]
    missing = [s for s in wanted if s not in on_map]
    unknown = [s for s in dict.fromkeys(on_map) if s not in wanted]
    return missing, unknown
