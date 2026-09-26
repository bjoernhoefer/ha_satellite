"""Source-Abstraktion für die verschiedenen EUMETSAT-Datenquellen.

Jede Quelle implementiert ``render(region, config, last_sensing) ->
RenderedFrame``. ``msg_seviri`` lädt echte Daten aus dem EUMETSAT Data
Store und rendert sie mit Satpy; ``data_tailor`` und ``mtg_fci`` liefern
bis zu ihrer Umsetzung noch Platzhalterbilder ("Dummy-Frames").
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from ha_satellite.config import AppConfig, RegionConfig

if TYPE_CHECKING:
    from ha_satellite.sources.eumetsat import ProductCache

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Wird ausgelöst, wenn ein Frame nicht erzeugt werden konnte."""


class NoNewData(Exception):
    """Seit dem letzten Frame gibt es keine neue Aufnahme - kein Fehler."""


@dataclass(frozen=True)
class RenderedFrame:
    png: bytes
    # Aufnahmezeitpunkt (Ende des Scans) - wird zum Frame-Zeitstempel.
    sensing_time: datetime


def data_dir() -> Path:
    return Path(os.environ.get("HA_SATELLITE_DATA_DIR", "/data"))


class Source(ABC):
    """Basisklasse für austauschbare Bildquellen."""

    name: str = "base"

    @abstractmethod
    def render(
        self,
        region: RegionConfig,
        config: AppConfig,
        last_sensing: datetime | None = None,
    ) -> RenderedFrame:
        """Erzeugt einen Frame für die Region.

        ``last_sensing`` ist der Aufnahmezeitpunkt des neuesten Frames im
        Puffer; liegt keine neuere Aufnahme vor, wird ``NoNewData``
        ausgelöst.
        """
        raise NotImplementedError

    def _dummy(self, region: RegionConfig) -> RenderedFrame:
        return RenderedFrame(self._dummy_frame(region), datetime.now(timezone.utc))

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

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        return self._dummy(region)


_product_caches: dict[Path, ProductCache] = {}


def _product_cache() -> ProductCache:
    from ha_satellite.sources.eumetsat import ProductCache

    cache_dir = data_dir() / "cache"
    if cache_dir not in _product_caches:
        _product_caches[cache_dir] = ProductCache(cache_dir)
    return _product_caches[cache_dir]


class MsgSeviriSource(Source):
    """MSG/SEVIRI aus dem Data Store + lokales Satpy-Rendering.

    Ein Produkt (~100 MB) wird einmal geladen und von allen Regionen
    genutzt; gerendert wird in einem Kindprozess.
    """

    name = "msg_seviri"
    reader = "seviri_l1b_native"
    entry_suffix = ".nat"

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        from ha_satellite.sources.eumetsat import DataStoreError
        from ha_satellite.sources.satpy_render import (
            RenderRequest,
            SatpyRenderError,
            render_in_subprocess,
        )

        try:
            product = _product_cache().latest(
                config.sources.collection_for(region.source), config.eumetsat, self.entry_suffix
            )
        except DataStoreError as exc:
            raise RenderError(str(exc)) from exc
        if last_sensing is not None and product.sensing_end <= last_sensing:
            raise NoNewData(f"{product.product_id} bereits gerendert")

        request = RenderRequest(
            reader=self.reader,
            filenames=(str(product.path),),
            composite=region.composite,
            lat=region.lat,
            lon=region.lon,
            radius_km=region.radius_km,
            width=region.width,
            height=region.height,
            label=region.name,
        )
        try:
            png, sensing_end = render_in_subprocess(request)
        except SatpyRenderError as exc:
            raise RenderError(str(exc)) from exc
        if sensing_end.tzinfo is None:
            sensing_end = sensing_end.replace(tzinfo=timezone.utc)
        return RenderedFrame(png, sensing_end)


class DataTailorSource(Source):
    """Serverseitiger Zuschnitt via EUMETSAT Data Tailor (folgt)."""

    name = "data_tailor"

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        logger.warning(
            "data_tailor: Anbindung noch nicht implementiert, liefere Dummy-Frame für %s",
            region.name,
        )
        return self._dummy(region)


class MtgFciSource(Source):
    """MTG/FCI-Chunks mit höherer Auflösung (folgt)."""

    name = "mtg_fci"

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        logger.warning(
            "mtg_fci: Anbindung noch nicht implementiert, liefere Dummy-Frame für %s",
            region.name,
        )
        return self._dummy(region)


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
