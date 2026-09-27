"""Erzeugt ``src/ha_satellite/overlay_data/borders_10m.json.gz``.

Quelle: Natural Earth 1:10m "Admin 0 - Boundary Lines" (Staatsgrenzen) und
"Admin 1 - States, Provinces" Lines (Bundesländer, nur ``STATE_COUNTRIES``),
gemeinfrei (https://www.naturalearthdata.com/about/terms-of-use/).

    python scripts/build_borders.py ne_10m_admin_0_boundary_lines_land.geojson \
        ne_10m_admin_1_states_provinces_lines.geojson

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


# Länder, deren innere Verwaltungsgrenzen (Bundesländer) mitkommen.
STATE_COUNTRIES = {"AUT"}


def _read_lines(source: str, keep=lambda props: True) -> list:
    features = json.loads(Path(source).read_text(encoding="utf-8"))["features"]
    lines = []
    for feature in features:
        if not keep(feature["properties"]):
            continue
        for coords in _lines(feature["geometry"]):
            points = []
            for lon, lat, *_ in coords:
                point = [round(lon, 3), round(lat, 3)]
                if not points or points[-1] != point:
                    points.append(point)
            if len(points) >= 2:
                lines.append(points)
    return lines


def main(countries: str, states: str) -> None:
    lines = _read_lines(countries)
    state_lines = _read_lines(states, lambda props: props.get("ADM0_A3") in STATE_COUNTRIES)
    payload = json.dumps(
        {
            "source": "Natural Earth 1:10m admin_0_boundary_lines_land + admin_1_states_provinces_lines",
            "state_countries": sorted(STATE_COUNTRIES),
            "lines": lines,
            "state_lines": state_lines,
        },
        separators=(",", ":"),
    )
    TARGET.write_bytes(gzip.compress(payload.encode(), 9, mtime=0))
    print(f"{len(lines)} + {len(state_lines)} Linien, {TARGET.stat().st_size // 1024} KB -> {TARGET}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
