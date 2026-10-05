from importlib.resources import files

from PIL import Image

from ha_satellite.config import RegionConfig
from ha_satellite.overlay import border_segments, draw_borders

VIENNA = (48.21, 16.37, 250)


def test_border_data_is_packaged():
    assert files("ha_satellite").joinpath("overlay_data/borders_10m.json.gz").is_file()


def test_vienna_has_borders_near_bratislava():
    segments = border_segments(*VIENNA, 800, 800)
    assert segments
    points = [p for segment in segments for p in segment]
    # The AT/SK border is ~50 km east of Vienna (radius 250 km -> 1.6 px/km):
    # just right of the image center, at mid height.
    assert any(460 < x < 500 and 380 < y < 420 for x, y in points)
    # No outliers far outside the image.
    assert all(-800 <= x <= 1600 and -800 <= y <= 1600 for x, y in points)


def test_island_without_land_borders_stays_empty():
    assert border_segments(39.6, 2.9, 100, 800, 800) == []  # Mallorca


def test_austrian_states_near_vienna():
    points = [p for s in border_segments(*VIENNA, 800, 800, kind="state_lines") for p in s]
    # Vienna is its own state: border with Lower Austria around the image center.
    assert any(abs(x - 400) < 15 and abs(y - 400) < 15 for x, y in points)


def test_administrative_borders_are_available_worldwide():
    # California has internal state/county borders, despite not being in Europe.
    assert border_segments(37.25, -119.5, 400, 800, 800, kind="state_lines")


def test_draw_borders_draws_thin_dark_lines():
    background = (120, 200, 80)
    image = Image.new("RGB", (800, 800), background)
    draw_borders(image, *VIENNA)
    assert image.mode == "RGB"
    pixels = list(image.getdata())
    changed = [p for p in pixels if p != background]
    # Black hairlines: only darkened, never lighter or colored.
    assert changed and all(p[0] <= 120 and p[1] <= 200 and p[2] <= 80 for p in changed)
    assert any(sum(p) < 100 for p in changed)
    # Thin: roughly one pixel wide along the lines, no wide edge.
    assert len(changed) < 0.06 * len(pixels)


def test_region_draws_borders_by_default():
    assert RegionConfig(name="x", lat=48.2, lon=16.4, radius_km=100).borders is True


def test_archive_signature_depends_on_borders():
    from ha_satellite.archive import region_signature

    region = RegionConfig(name="wien", lat=48.2, lon=16.4, radius_km=100)
    without = region.model_copy(update={"borders": False})
    assert region_signature(region) != region_signature(without)
