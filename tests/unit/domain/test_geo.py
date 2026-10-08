import pytest

from tg_discovery.domain.geo import BoundingBox, geohash_encode, haversine_m


def test_haversine_london_to_paris() -> None:
    assert haversine_m(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(343_500, abs=1_500)


def test_haversine_of_the_same_point_is_zero() -> None:
    assert haversine_m(13.75, 100.5, 13.75, 100.5) == 0


@pytest.mark.parametrize(
    ("lat", "lng", "precision", "expected"),
    [
        (57.64911, 10.40744, 11, "u4pruydqqvj"),  # worked example from the geohash specification
        (42.60498, -5.60303, 5, "ezs42"),
    ],
)
def test_geohash_known_vectors(lat: float, lng: float, precision: int, expected: str) -> None:
    assert geohash_encode(lat, lng, precision) == expected


def test_geohash_precision_7_is_stable() -> None:
    """Regression guard for the cell size used in fingerprints (the value comes from this implementation)."""
    assert geohash_encode(13.7563, 100.5018) == "w4rqqbr"  # Bangkok


def test_nearby_points_share_a_geohash_prefix() -> None:
    assert geohash_encode(13.7563, 100.5018)[:6] == geohash_encode(13.7566, 100.5021)[:6]


def test_bounding_box_contains_and_margin() -> None:
    box = BoundingBox(south=13.5, west=100.3, north=14.0, east=100.9)

    assert box.contains(13.75, 100.5)
    assert not box.contains(14.05, 100.5)
    assert box.contains(14.05, 100.5, margin_ratio=0.2)  # 0.2 of the 0.5 degree height is 0.1 degree


def test_bounding_box_around_a_point_covers_its_radius() -> None:
    box = BoundingBox.around(13.75, 100.5, radius_km=10)

    assert box.contains(13.75, 100.5)
    assert box.contains(13.75 + 0.08, 100.5)
    assert not box.contains(13.75 + 0.12, 100.5)
    assert box.center == pytest.approx((13.75, 100.5))


def test_bounding_box_radius_reaches_the_corner() -> None:
    box = BoundingBox.around(0.0, 0.0, radius_km=10)

    assert box.radius_km == pytest.approx(10 * 2**0.5, rel=0.01)
