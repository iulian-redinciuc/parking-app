"""Small shapely helpers shared by occupancy scoring, counting and drawing.

Boxes are `(x1, y1, x2, y2)` in pixels; points are `(x, y)`.
"""

from collections.abc import Iterable, Sequence

import shapely
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry

Box = tuple[float, float, float, float]


def to_polygon(points: Iterable[Sequence[float]]) -> BaseGeometry:
    """A polygon from `[x, y]` points, repaired with `.buffer(0)` to make it valid.

    A self-intersecting outline may lose a lobe or come out empty; callers use only its area
    and intersections.
    """
    return Polygon([(float(x), float(y)) for x, y in points]).buffer(0)


def box_bottom(box: Box, frac: float = 0.35) -> Polygon:
    """The bottom `frac` of a box: roughly where the vehicle touches the ground."""
    x1, y1, x2, y2 = box
    return shapely.box(x1, y2 - frac * (y2 - y1), x2, y2)


def overlap_ratio(footprint: BaseGeometry, slot: BaseGeometry) -> float:
    """area(footprint ∩ slot) / area(slot); 0 for an empty slot."""
    if slot.area <= 0:
        return 0.0
    return footprint.intersection(slot).area / slot.area


def bottom_center(box: Box) -> tuple[float, float]:
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2, y2)


def point_in(poly: BaseGeometry, pt: Sequence[float]) -> bool:
    """True if `pt` is inside `poly` or on its edge."""
    return poly.covers(Point(pt[0], pt[1]))
