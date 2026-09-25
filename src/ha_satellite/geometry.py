"""Geometrie-Hilfsfunktionen für Regionen.

Berechnet aus Mittelpunkt (Lat/Lon) und Umkreis (km) eine einfache
äquidistante Bounding-Box in Grad. Die Näherung nutzt die klassische
"Grad pro Kilometer"-Umrechnung (1° Breite ≈ 111,32 km) und korrigiert
die Längengrad-Ausdehnung mit dem Kosinus der Breite. Für die Zwecke
dieses Projekts (Zuschnitt von Satellitenbildern auf einige hundert
Kilometer) ist diese sphärische Näherung ausreichend genau und kommt
ohne zusätzliche Abhängigkeiten (z. B. pyproj) aus.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KM_PER_DEGREE_LAT = 111.32
# Kleinster Wert, unterhalb dessen wir cos(lat) nicht mehr als Divisor
# verwenden (Vermeidung einer Division durch (nahezu) Null an den Polen).
_MIN_COS_LAT = 1e-6


@dataclass(frozen=True)
class BoundingBox:
    """Bounding-Box in Grad, WGS84."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.lat_min, self.lon_min, self.lat_max, self.lon_max)


def bounding_box(lat: float, lon: float, radius_km: float) -> BoundingBox:
    """Berechnet eine Bounding-Box um (lat, lon) mit gegebenem Umkreis in km.

    Args:
        lat: Breitengrad des Mittelpunkts in Grad (-90..90).
        lon: Längengrad des Mittelpunkts in Grad (-180..180).
        radius_km: Umkreis in Kilometern (muss > 0 sein).

    Returns:
        BoundingBox mit lat_min/lon_min/lat_max/lon_max in Grad.
    """
    if radius_km <= 0:
        raise ValueError("radius_km muss größer als 0 sein")
    if not -90.0 <= lat <= 90.0:
        raise ValueError("lat muss zwischen -90 und 90 liegen")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("lon muss zwischen -180 und 180 liegen")

    delta_lat = radius_km / KM_PER_DEGREE_LAT
    cos_lat = max(abs(math.cos(math.radians(lat))), _MIN_COS_LAT)
    delta_lon = radius_km / (KM_PER_DEGREE_LAT * cos_lat)

    lat_min = max(lat - delta_lat, -90.0)
    lat_max = min(lat + delta_lat, 90.0)
    lon_min = lon - delta_lon
    lon_max = lon + delta_lon

    return BoundingBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
