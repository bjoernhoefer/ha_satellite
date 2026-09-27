from importlib.resources import files

from PIL import Image

from ha_satellite.config import RegionConfig
from ha_satellite.overlay import border_segments, draw_borders

WIEN = (48.21, 16.37, 250)


def test_border_data_is_packaged():
    assert files("ha_satellite").joinpath("overlay_data/borders_10m.json.gz").is_file()


def test_wien_has_borders_near_bratislava():
    segments = border_segments(*WIEN, 800, 800)
    assert segments
    points = [p for segment in segments for p in segment]
    # Grenze AT/SK liegt ~50 km östlich von Wien (Radius 250 km -> 1,6 px/km):
    # knapp rechts der Bildmitte, auf Höhe der Mitte.
    assert any(460 < x < 500 and 380 < y < 420 for x, y in points)
    # Keine Ausreißer weit außerhalb des Bildes.
    assert all(-800 <= x <= 1600 and -800 <= y <= 1600 for x, y in points)


def test_island_without_land_borders_stays_empty():
    assert border_segments(39.6, 2.9, 100, 800, 800) == []  # Mallorca


def test_austrian_states_near_wien():
    points = [p for s in border_segments(*WIEN, 800, 800, kind="state_lines") for p in s]
    # Wien ist ein eigenes Bundesland: Grenze zu Niederösterreich rund um die Bildmitte.
    assert any(abs(x - 400) < 15 and abs(y - 400) < 15 for x, y in points)
    # Nur Österreich: in Mallorca keine Verwaltungsgrenzen.
    assert border_segments(39.6, 2.9, 100, 800, 800, kind="state_lines") == []


def test_draw_borders_draws_thin_translucent_lines():
    background = (40, 60, 90)
    image = Image.new("RGB", (800, 800), background)
    draw_borders(image, *WIEN)
    colors = {color for _, color in image.getcolors(maxcolors=100000)}
    assert len(colors) >= 3  # Hintergrund, Staats- und Bundesländergrenzen
    # Transparent: kein volles Gelb, kein schwarzer Rand mehr.
    assert (255, 215, 0) not in colors
    assert (0, 0, 0) not in colors
    assert image.mode == "RGB"


def test_region_draws_borders_by_default():
    assert RegionConfig(name="x", lat=48.2, lon=16.4, radius_km=100).borders is True


def test_archive_cache_path_depends_on_borders(tmp_path):
    from datetime import datetime, timezone

    from ha_satellite.sources.fci_archive import Slot, render_cache_path

    now = datetime(2026, 9, 26, 14, 20, tzinfo=timezone.utc)
    slot = Slot("20260926T142000Z", "EO:EUM:DAT:0662", "p", now, now, tmp_path)
    region = RegionConfig(name="wien", lat=48.2, lon=16.4, radius_km=100)
    without = region.model_copy(update={"borders": False})
    assert render_cache_path(slot, region, "cloudtop") != render_cache_path(slot, without, "cloudtop")
