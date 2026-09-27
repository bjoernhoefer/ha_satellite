"""Landesgrenzen als Overlay über ein gerendertes Regionsbild.

Die Grenzlinien stammen aus Natural Earth 1:10m (gemeinfrei, erzeugt mit
``scripts/build_borders.py``) und liegen gepackt im Paket. Projiziert wird
in dieselbe Lambert-Azimutal-Projektion wie die Zielregion
(``satpy_render.target_area``), dadurch passen Linien und Bild exakt
übereinander - ohne pycoast und dessen ~150-MB-GSHHS-Datensatz.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

BORDERS_FILE = Path(__file__).resolve().parent / "overlay_data" / "borders_10m.json.gz"
BORDER_COLOR = (255, 215, 0)
SHADOW_COLOR = (0, 0, 0)
# Punkte weiter als dieses Vielfache des Radius vom Mittelpunkt werden
# verworfen (Linie wird dort aufgetrennt) - hält die Pixelwerte klein.
_CLIP_FACTOR = 1.5


@lru_cache(maxsize=1)
def _border_lines():
    import numpy as np

    data = json.loads(gzip.decompress(BORDERS_FILE.read_bytes()))
    lines = [np.asarray(line, dtype=float) for line in data["lines"]]
    boxes = np.array([(l[:, 0].min(), l[:, 1].min(), l[:, 0].max(), l[:, 1].max()) for l in lines])
    return lines, boxes


def border_segments(lat: float, lon: float, radius_km: float, width: int, height: int):
    """Grenzlinien als Pixel-Polylinien [(x, y), ...] im Zielbild."""
    import numpy as np
    from pyproj import Transformer

    from ha_satellite.geometry import bounding_box

    lines, boxes = _border_lines()
    box = bounding_box(lat, min(max(lon, -180.0), 180.0), radius_km * _CLIP_FACTOR)
    hit = (
        (boxes[:, 2] >= box.lon_min) & (boxes[:, 0] <= box.lon_max)
        & (boxes[:, 3] >= box.lat_min) & (boxes[:, 1] <= box.lat_max)
    )
    if not hit.any():
        return []
    transformer = Transformer.from_crs(
        "EPSG:4326",
        {"proj": "laea", "lat_0": lat, "lon_0": lon, "ellps": "WGS84"},
        always_xy=True,
    )
    radius_m = radius_km * 1000
    limit = radius_m * _CLIP_FACTOR
    segments = []
    for index in np.flatnonzero(hit):
        line = lines[index]
        x, y = transformer.transform(line[:, 0], line[:, 1])
        inside = np.isfinite(x) & np.isfinite(y) & (np.abs(x) <= limit) & (np.abs(y) <= limit)
        cols = (x + radius_m) / (2 * radius_m) * width
        rows = (radius_m - y) / (2 * radius_m) * height
        # Zusammenhängende Abschnitte innerhalb des Clip-Bereichs.
        start = None
        for i, ok in enumerate(np.append(inside, False)):
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if i - start >= 2:
                    segments.append(list(zip(cols[start:i].tolist(), rows[start:i].tolist())))
                start = None
    return segments


def draw_borders(image, lat: float, lon: float, radius_km: float):
    """Zeichnet die Landesgrenzen (gelb mit dunklem Rand) in ein PIL-Bild."""
    from PIL import ImageDraw

    segments = border_segments(lat, lon, radius_km, image.width, image.height)
    if not segments:
        return image
    line_width = max(1, round(min(image.width, image.height) / 700))
    draw = ImageDraw.Draw(image)
    for segment in segments:
        draw.line(segment, fill=SHADOW_COLOR, width=line_width + 2, joint="curve")
    for segment in segments:
        draw.line(segment, fill=BORDER_COLOR, width=line_width, joint="curve")
    return image


__all__ = ["border_segments", "draw_borders"]
