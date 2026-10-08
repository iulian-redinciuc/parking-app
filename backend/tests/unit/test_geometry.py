import pytest

from parking.geometry import bottom_center, box_bottom, overlap_ratio, point_in, to_polygon


def test_to_polygon_repairs_bow_tie():
    poly = to_polygon([[0, 0], [10, 10], [10, 0], [0, 10]])  # self-intersecting
    # buffer(0) gives a valid shape (it keeps one lobe of a bow-tie)
    assert poly.is_valid
    assert 0 < poly.area <= 50


def test_to_polygon_simple():
    assert to_polygon([[0, 0], [4, 0], [4, 3], [0, 3]]).area == pytest.approx(12)


def test_box_bottom_default_and_custom_frac():
    assert box_bottom((0, 0, 10, 100)).bounds == pytest.approx((0, 65, 10, 100))
    assert box_bottom((0, 0, 10, 100), frac=0.5).bounds == pytest.approx((0, 50, 10, 100))


def test_overlap_ratio():
    slot = to_polygon([[0, 0], [10, 0], [10, 10], [0, 10]])
    assert overlap_ratio(to_polygon([[5, 0], [20, 0], [20, 10], [5, 10]]), slot) == 0.5
    assert overlap_ratio(to_polygon([[20, 20], [30, 20], [30, 30]]), slot) == 0
    assert overlap_ratio(slot, to_polygon([[0, 0], [1, 1], [2, 2]])) == 0  # empty slot


def test_bottom_center_and_point_in():
    assert bottom_center((10, 20, 30, 60)) == (20, 60)
    sq = to_polygon([[0, 0], [10, 0], [10, 10], [0, 10]])
    assert point_in(sq, (5, 5))
    assert point_in(sq, (10, 5))  # on the edge counts
    assert not point_in(sq, (11, 5))
