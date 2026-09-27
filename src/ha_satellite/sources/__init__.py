"""Source-Abstraktion für die verschiedenen EUMETSAT-Datenquellen.

Jede Quelle implementiert ``render(region, config, last_sensing) ->
RenderedFrame``. ``msg_seviri`` und ``mtg_fci`` laden echte Daten aus dem
EUMETSAT Data Store und rendern sie mit Satpy; ``data_tailor`` liefert
bis zu seiner Umsetzung noch Platzhalterbilder ("Dummy-Frames").
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
    from ha_satellite.config import SourceDefinition
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
    # Eigener Download-Schritt (fetch) im Takt der Quelle?
    downloads: bool = False
    # Auch herunterladen, wenn keine Region die Quelle nutzt (Rohdaten-Archiv).
    archive_always: bool = False

    def fetch(self, entry: "SourceDefinition", config: AppConfig) -> datetime:
        """Neuestes Produkt lokal bereitstellen (idempotent, ohne Rendern).

        Liefert das Aufnahmeende des neuesten lokal vorhandenen Produkts.
        """
        raise NotImplementedError

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
    downloads = True

    def _download(self, collection: str, config: AppConfig):
        from ha_satellite.sources.eumetsat import DataStoreError

        try:
            return _product_cache().latest(collection, config.eumetsat, self.entry_suffix)
        except DataStoreError as exc:
            raise RenderError(str(exc)) from exc

    def fetch(self, entry, config) -> datetime:
        return self._download(entry.collection or "", config).sensing_end

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        from ha_satellite.sources.satpy_render import (
            RenderRequest,
            SatpyRenderError,
            render_in_subprocess,
        )

        collection = config.sources.collection_for(region.source)
        # Normalerweise hat der Download-Job das Produkt gerade geladen;
        # nur ohne lokales Produkt (z. B. direkt nach dem Start) selbst laden.
        product = _product_cache().cached(collection) or self._download(collection, config)
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
            borders=region.borders,
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


def frames_root(config: AppConfig) -> Path:
    return Path(config.storage.frames_dir) if config.storage.frames_dir else data_dir() / "frames"


def archive_root(config: AppConfig) -> Path:
    from ha_satellite.sources.fci_archive import ARCHIVE_SUBDIR

    return frames_root(config) / ARCHIVE_SUBDIR


def fci_source_for(config: AppConfig, region: RegionConfig | None = None):
    """FCI-Katalogeintrag für eine Region: ihre eigene Quelle, falls FCI,
    sonst der erste aktivierte (bzw. erste) FCI-Eintrag."""
    if region is not None:
        own = config.sources.get(region.source)
        if own is not None and own.driver == "mtg_fci":
            return own
    entries = [e for e in config.sources.catalog if e.driver == "mtg_fci"]
    return next((e for e in entries if e.enabled), entries[0] if entries else None)


def render_fci_slot(slot, region: RegionConfig, composite: str) -> RenderedFrame:
    """Rendert eine Region aus einem archivierten FCI-Slot."""
    from ha_satellite.config import resolve_fci_composite
    from ha_satellite.sources.fci_archive import chunks_for_region, format_chunks
    from ha_satellite.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        render_in_subprocess,
    )

    needed = chunks_for_region(region)
    if not needed:
        raise RenderError(f"Region {region.name} liegt außerhalb der FCI-Vollscheibe")
    files = slot.chunk_files()
    missing = needed - set(files)
    if missing:
        raise RenderError(
            f"Slot {slot.name} enthält nicht alle Chunks für {region.name} "
            f"(benötigt {format_chunks(needed)}, fehlt {format_chunks(missing)})"
        )
    request = RenderRequest(
        reader="fci_l1c_nc",
        filenames=tuple(str(files[c]) for c in sorted(needed)),
        composite=resolve_fci_composite(composite),
        lat=region.lat,
        lon=region.lon,
        radius_km=region.radius_km,
        width=region.width,
        height=region.height,
        label=region.name,
        borders=region.borders,
    )
    try:
        png, sensing_end = render_in_subprocess(request)
    except SatpyRenderError as exc:
        raise RenderError(str(exc)) from exc
    if sensing_end.tzinfo is None:
        sensing_end = sensing_end.replace(tzinfo=timezone.utc)
    return RenderedFrame(png, sensing_end)


def sync_fci_archive(config: AppConfig, collection: str):
    """Neuesten FCI-Slot ins Archiv laden (idempotent), liefert den Slot."""
    from ha_satellite.config import DEFAULT_FCI_COLLECTION
    from ha_satellite.sources.fci_archive import ArchiveError, get_archive, wanted_chunks

    try:
        return get_archive(archive_root(config)).sync(
            collection or DEFAULT_FCI_COLLECTION,
            config.eumetsat,
            wanted_chunks(config),
            config.archive.retention_hours,
        )
    except ArchiveError as exc:
        raise RenderError(str(exc)) from exc


class MtgFciSource(Source):
    """MTG/FCI (1 km sichtbar, 2 km IR) aus dem Chunk-Archiv.

    Pro Slot werden nur die benötigten Chunks geladen (Europa ~180 MB statt
    ~1 GB) und für ``archive.retention_hours`` aufbewahrt; gerendert werden
    nur die Chunks der Region (hält den Speicher unter ~500 MB).
    """

    name = "mtg_fci"
    downloads = True
    archive_always = True

    def fetch(self, entry, config) -> datetime:
        return sync_fci_archive(config, entry.collection or "").sensing_end

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        from ha_satellite.config import DEFAULT_FCI_COLLECTION
        from ha_satellite.sources.fci_archive import chunks_for_region, get_archive

        collection = config.sources.collection_for(region.source, DEFAULT_FCI_COLLECTION)
        # Neuester archivierter Slot mit allen Chunks der Region (vom
        # Download-Job geladen); fehlt er, selbst laden.
        needed = chunks_for_region(region)
        slot = next(
            (s for s in get_archive(archive_root(config)).slots(collection)[:1]
             if needed and needed <= set(s.chunk_files())),
            None,
        ) or sync_fci_archive(config, collection)
        if last_sensing is not None and slot.sensing_end <= last_sensing:
            raise NoNewData(f"{slot.product_id} bereits gerendert")
        return render_fci_slot(slot, region, region.composite)


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
