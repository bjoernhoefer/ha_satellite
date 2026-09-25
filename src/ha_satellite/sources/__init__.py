"""Source-Abstraktion für die verschiedenen EUMETSAT-Datenquellen.

Jede Quelle implementiert ``render(region, credentials) -> PNG-Bytes``.
In dieser Phase des Projekts (Grundgerüst) liefern alle Quellen ein
Platzhalterbild ("Dummy-Frame") mit Zeitstempel, Regionsname und
Bounding-Box, damit Web-Server, Konfiguration, Ringpuffer und Endpunkte
bereits vollständig durchspielbar sind. Die eigentliche EUMETSAT/Satpy-
Kette wird pro Quelle in einer späteren Phase in ``render()`` ergänzt,
ohne dass sich an der Schnittstelle etwas ändert.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw

from ha_satellite.config import EumetsatCredentials, RegionConfig

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Wird ausgelöst, wenn ein Frame nicht erzeugt werden konnte."""


class Source(ABC):
    """Basisklasse für austauschbare Bildquellen."""

    name: str = "base"

    @abstractmethod
    def render(self, region: RegionConfig, credentials: EumetsatCredentials) -> bytes:
        """Erzeugt ein PNG-Bild (als Bytes) für die gegebene Region."""
        raise NotImplementedError

    def _dummy_frame(self, region: RegionConfig) -> bytes:
        """Gemeinsamer Platzhalter-Renderer, bis die echte Quelle steht."""
        bbox = region.bounding_box()
        image = Image.new("RGB", (region.width, region.height), color=(10, 20, 40))
        draw = ImageDraw.Draw(image)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            f"ha_satellite ({self.name})",
            f"Region: {region.name}",
            f"Komposit: {region.composite}",
            f"BBox: {bbox.lat_min:.2f},{bbox.lon_min:.2f} .. {bbox.lat_max:.2f},{bbox.lon_max:.2f}",
            timestamp,
        ]
        y = 20
        for line in lines:
            draw.text((20, y), line, fill=(255, 255, 255))
            y += 24
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class DummySource(Source):
    """Reine Testquelle, die ausschließlich Platzhalterbilder erzeugt."""

    name = "dummy"

    def render(self, region: RegionConfig, credentials: EumetsatCredentials) -> bytes:
        return self._dummy_frame(region)


class MsgSeviriSource(Source):
    """Full-Disk-Download + lokales Satpy-Rendering (folgt in Phase 2)."""

    name = "msg_seviri"

    def render(self, region: RegionConfig, credentials: EumetsatCredentials) -> bytes:
        logger.warning(
            "msg_seviri: Satpy-Kette noch nicht implementiert, liefere Dummy-Frame für %s",
            region.name,
        )
        return self._dummy_frame(region)


class DataTailorSource(Source):
    """Serverseitiger Zuschnitt via EUMETSAT Data Tailor (folgt in Phase 2)."""

    name = "data_tailor"

    def render(self, region: RegionConfig, credentials: EumetsatCredentials) -> bytes:
        logger.warning(
            "data_tailor: Anbindung noch nicht implementiert, liefere Dummy-Frame für %s",
            region.name,
        )
        return self._dummy_frame(region)


class MtgFciSource(Source):
    """MTG/FCI-Chunks mit höherer Auflösung (folgt in Phase 2)."""

    name = "mtg_fci"

    def render(self, region: RegionConfig, credentials: EumetsatCredentials) -> bytes:
        logger.warning(
            "mtg_fci: Anbindung noch nicht implementiert, liefere Dummy-Frame für %s",
            region.name,
        )
        return self._dummy_frame(region)


SOURCE_REGISTRY: dict[str, type[Source]] = {
    "msg_seviri": MsgSeviriSource,
    "data_tailor": DataTailorSource,
    "mtg_fci": MtgFciSource,
    "dummy": DummySource,
}


def get_source(name: str) -> Source:
    try:
        return SOURCE_REGISTRY[name]()
    except KeyError as exc:
        raise RenderError(f"Unbekannte Quelle: {name}") from exc
