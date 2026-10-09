"""Generates ``src/ha_satimage/overlay_data/europe_land_50m.json.gz``.

Land polygons for the location map of Europe (web UI, "location_change").
Source: Natural Earth 1:50m "Land", public domain
(https://www.naturalearthdata.com/about/terms-of-use/).

    python scripts/build_europe_land.py ne_50m_land.geojson

Every ring is clipped to ``EXTENT`` (plus a small margin), coordinates are
rounded to 2 decimal places (~1 km) and consecutive duplicate points are
dropped. Interior rings (e.g. the Caspian Sea) are kept as ``holes``.
"""

import gzip
import json
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src/ha_satimage/overlay_data/europe_land_50m.json.gz"

# Must cover ha_satimage.location_change.EUROPE_EXTENT (lon_min, lat_min, lon_max, lat_max).
EXTENT = (-27.0, 31.0, 47.0, 74.0)


def _clip(ring, extent):
    """Sutherland-Hodgman: clip a closed ring to an axis-aligned rectangle."""
    lon_min, lat_min, lon_max, lat_max = extent
    edges = (
        (lambda p: p[0] >= lon_min, lambda a, b: _cut_x(a, b, lon_min)),
        (lambda p: p[0] <= lon_max, lambda a, b: _cut_x(a, b, lon_max)),
        (lambda p: p[1] >= lat_min, lambda a, b: _cut_y(a, b, lat_min)),
        (lambda p: p[1] <= lat_max, lambda a, b: _cut_y(a, b, lat_max)),
    )
    points = list(ring)
    for inside, cut in edges:
        if not points:
            break
        result = []
        previous = points[-1]
        for current in points:
            if inside(current):
                if not inside(previous):
                    result.append(cut(previous, current))
                result.append(current)
            elif inside(previous):
                result.append(cut(previous, current))
            previous = current
        points = result
    return points


def _cut_x(a, b, x):
    t = (x - a[0]) / (b[0] - a[0])
    return (x, a[1] + t * (b[1] - a[1]))


def _cut_y(a, b, y):
    t = (y - a[1]) / (b[1] - a[1])
    return (a[0] + t * (b[0] - a[0]), y)


def _round(ring):
    points = []
    for lon, lat in ring:
        point = [round(lon, 2), round(lat, 2)]
        if not points or points[-1] != point:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points if len(points) >= 3 else None


def _polygons(geometry):
    if geometry["type"] == "Polygon":
        yield geometry["coordinates"]
    elif geometry["type"] == "MultiPolygon":
        yield from geometry["coordinates"]


def main(source: str) -> None:
    features = json.loads(Path(source).read_text(encoding="utf-8"))["features"]
    polygons = []
    for feature in features:
        for rings in _polygons(feature["geometry"]):
            outer = _round(_clip([tuple(p[:2]) for p in rings[0]], EXTENT))
            if outer is None:
                continue
            holes = [
                hole for hole in (_round(_clip([tuple(p[:2]) for p in ring], EXTENT)) for ring in rings[1:])
                if hole is not None
            ]
            polygons.append({"outer": outer, "holes": holes})
    payload = json.dumps(
        {"source": "Natural Earth 1:50m land", "extent": EXTENT, "polygons": polygons},
        separators=(",", ":"),
    ).encode()
    TARGET.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    points = sum(len(p["outer"]) + sum(len(h) for h in p["holes"]) for p in polygons)
    print(f"{TARGET}: {len(polygons)} polygons, {points} points, {TARGET.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(*sys.argv[1:])
