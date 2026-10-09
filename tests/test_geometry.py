import math

import pytest

from ha_satimage.geometry import bounding_box


def test_bounding_box_symmetric_at_equator():
    bbox = bounding_box(lat=0.0, lon=0.0, radius_km=111.32)
    assert bbox.lat_min == pytest.approx(-1.0, abs=1e-6)
    assert bbox.lat_max == pytest.approx(1.0, abs=1e-6)
    assert bbox.lon_min == pytest.approx(-1.0, abs=1e-6)
    assert bbox.lon_max == pytest.approx(1.0, abs=1e-6)


def test_bounding_box_widens_longitude_at_higher_latitude():
    bbox_equator = bounding_box(lat=0.0, lon=0.0, radius_km=200)
    bbox_vienna = bounding_box(lat=48.2082, lon=16.3738, radius_km=200)

    lon_span_equator = bbox_equator.lon_max - bbox_equator.lon_min
    lon_span_vienna = bbox_vienna.lon_max - bbox_vienna.lon_min

    # For the same radius the longitude span must grow with latitude
    # (1 / cos(lat)).
    assert lon_span_vienna > lon_span_equator
    expected_ratio = 1 / math.cos(math.radians(48.2082))
    assert lon_span_vienna / lon_span_equator == pytest.approx(expected_ratio, rel=1e-3)


def test_bounding_box_clamps_latitude_near_pole():
    bbox = bounding_box(lat=89.5, lon=10.0, radius_km=200)
    assert bbox.lat_max <= 90.0


@pytest.mark.parametrize(
    "lat, lon, radius_km",
    [
        (0, 0, 0),
        (0, 0, -5),
        (91, 0, 100),
        (-91, 0, 100),
        (0, 181, 100),
        (0, -181, 100),
    ],
)
def test_bounding_box_rejects_invalid_input(lat, lon, radius_km):
    with pytest.raises(ValueError):
        bounding_box(lat=lat, lon=lon, radius_km=radius_km)
