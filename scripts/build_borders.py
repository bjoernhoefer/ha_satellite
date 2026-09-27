"""Erzeugt ``src/ha_satellite/overlay_data/borders_10m.json.gz``.

Quelle: Natural Earth 1:10m "Admin 0 - Boundary Lines" (Land-Grenzen),
gemeinfrei (https://www.naturalearthdata.com/about/terms-of-use/).

    python scripts/build_borders.py ne_10m_admin_0_boundary_lines_land.geojson

Koordinaten werden auf 3 Nachkommastellen (~100 m) gerundet; aufeinander-
folgende gleiche Punkte entfallen.
"""

import gzip
import json
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src/ha_satellite/overlay_data/borders_10m.json.gz"


def _lines(geometry):
    if geometry["type"] == "LineString":
        yield geometry["coordinates"]
    elif geometry["type"] == "MultiLineString":
        yield from geometry["coordinates"]


def main(source: str) -> None:
    features = json.loads(Path(source).read_text(encoding="utf-8"))["features"]
    lines = []
    for feature in features:
        for coords in _lines(feature["geometry"]):
            points = []
            for lon, lat, *_ in coords:
                point = [round(lon, 3), round(lat, 3)]
                if not points or points[-1] != point:
                    points.append(point)
            if len(points) >= 2:
                lines.append(points)
    payload = json.dumps({"source": "Natural Earth 1:10m admin_0_boundary_lines_land", "lines": lines},
                         separators=(",", ":"))
    TARGET.write_bytes(gzip.compress(payload.encode(), 9, mtime=0))
    print(f"{len(lines)} Linien, {TARGET.stat().st_size // 1024} KB -> {TARGET}")


if __name__ == "__main__":
    main(sys.argv[1])
