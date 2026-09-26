"""Rohdaten-Archiv für MTG FCI: nur die benötigten Chunks je Slot.

Ein FCI-Slot (alle 10 Minuten) besteht aus 40 Streifen ("Chunks") der
Vollscheibe, nummeriert von Süd (1) nach Nord (40), zusammen ~1 GB. Für
Europa reichen die Chunks ~32-40 (~180 MB). Diese werden je Slot unter
``<Speicherort>/_archive/<Collection>/<Slot>/`` abgelegt und für
``retention_hours`` aufbewahrt, sodass Regionen und Komposite auch
nachträglich gerendert werden können (auch für neu angelegte Regionen).

Welche Chunks eine Region braucht, ergibt sich aus der Geometrie: die
Bounding Box wird in die geostationäre Projektion (Satellit über 0°)
umgerechnet, die Zeilen des 2-km-Grids (5568 Zeilen) bestimmen den Chunk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satellite.config import (
    FCI_CHUNK_COUNT,
    AppConfig,
    EumetsatCredentials,
    RegionConfig,
    resolve_fci_composite,
)

logger = logging.getLogger(__name__)

ARCHIVE_SUBDIR = "_archive"
META_FILENAME = "meta.json"
RENDERS_SUBDIR = "renders"
SLOT_FORMAT = "%Y%m%dT%H%M%SZ"
SEARCH_WINDOW = timedelta(hours=2)

# Geostationäre Projektion (CGMS/PROJ "geos", sweep=y) für MTG über 0°.
_SAT_HEIGHT = 35_786_400.0
_A = 6_378_137.0
_B = 6_356_752.31414
_GRID_LINES = 5568
_GRID_RES = 2000.0

_CHUNK_RE = re.compile(r"_(\d{4})\.nc$")
_SLOT_RE = re.compile(r"^\d{8}T\d{6}Z$")


class ArchiveError(Exception):
    """Fehler beim Zugriff auf den Data Store oder das Archiv."""


def _geos_y(lat: float, lon: float, lon_0: float = 0.0) -> float | None:
    """Nord-Süd-Koordinate (m) in der geos-Projektion; None = nicht sichtbar."""
    phi, lam = math.radians(lat), math.radians(lon - lon_0)
    radius_p, radius_g = _B / _A, 1.0 + _SAT_HEIGHT / _A
    lat_c = math.atan(radius_p * radius_p * math.tan(phi))
    r = radius_p / math.hypot(radius_p * math.cos(lat_c), math.sin(lat_c))
    vx = r * math.cos(lam) * math.cos(lat_c)
    vy = r * math.sin(lam) * math.cos(lat_c)
    vz = r * math.sin(lat_c)
    tmp = radius_g - vx
    # Punkt liegt auf der vom Satelliten abgewandten Seite der Erde.
    if (tmp * vx - vy * vy - vz * vz * (1.0 / (radius_p * radius_p))) < 0:
        return None
    return _SAT_HEIGHT * math.atan(vz / math.hypot(vy, tmp))


def chunk_for_y(y: float) -> int:
    row = (y + _GRID_LINES * _GRID_RES / 2) / _GRID_RES
    chunk = int(row // (_GRID_LINES / FCI_CHUNK_COUNT)) + 1
    return min(max(chunk, 1), FCI_CHUNK_COUNT)


def chunks_for_region(region: RegionConfig) -> set[int]:
    """Chunks, die die Bounding Box der Region abdecken (leer = nicht sichtbar)."""
    bbox = region.bounding_box()
    ys: list[float] = []
    steps = 24
    for i in range(steps + 1):
        lon = bbox.lon_min + (bbox.lon_max - bbox.lon_min) * i / steps
        lat = bbox.lat_min + (bbox.lat_max - bbox.lat_min) * i / steps
        for point in ((bbox.lat_min, lon), (bbox.lat_max, lon), (lat, bbox.lon_min), (lat, bbox.lon_max)):
            y = _geos_y(*point)
            if y is not None:
                ys.append(y)
    if not ys:
        return set()
    return set(range(chunk_for_y(min(ys)), chunk_for_y(max(ys)) + 1))


def wanted_chunks(config: AppConfig) -> set[int]:
    """Konfigurierter Bereich plus alle Chunks der konfigurierten Regionen."""
    chunks = set(range(config.archive.chunk_min, config.archive.chunk_max + 1))
    for region in config.regions:
        chunks |= chunks_for_region(region)
    return chunks


def format_chunks(chunks: set[int] | list[int]) -> str:
    """{32, 33, 34, 36} -> "32–34, 36"."""
    ordered = sorted(set(chunks))
    parts: list[str] = []
    start = prev = None
    for c in ordered + [None]:
        if start is None:
            start = prev = c
        elif c is not None and c == prev + 1:
            prev = c
        else:
            parts.append(str(start) if start == prev else f"{start}–{prev}")
            start = prev = c
    return ", ".join(parts) or "–"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _safe_name(collection_id: str) -> str:
    return collection_id.replace(":", "_")


def chunk_of(filename: str) -> int | None:
    if "TRAIL" in filename:
        return None
    match = _CHUNK_RE.search(filename)
    if not match:
        return None
    chunk = int(match.group(1))
    return chunk if 1 <= chunk <= FCI_CHUNK_COUNT else None


@dataclass(frozen=True)
class Slot:
    name: str
    collection: str
    product_id: str
    sensing_start: datetime
    sensing_end: datetime
    path: Path

    def chunk_files(self) -> dict[int, Path]:
        files: dict[int, Path] = {}
        if not self.path.is_dir():
            return files
        for entry in self.path.iterdir():
            chunk = chunk_of(entry.name) if entry.is_file() else None
            if chunk is not None:
                files[chunk] = entry
        return files

    @property
    def chunks(self) -> list[int]:
        return sorted(self.chunk_files())

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "collection": self.collection,
            "product_id": self.product_id,
            "sensing_start": self.sensing_start.isoformat(),
            "sensing_end": self.sensing_end.isoformat(),
            "chunks": self.chunks,
        }


class FciArchive:
    """Lädt je Slot die gewünschten Chunks und räumt nach Aufbewahrungszeit auf."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._status: dict[str, dict] = {}

    # -- Lesen -------------------------------------------------------------
    def collection_dir(self, collection: str) -> Path:
        return self.root / _safe_name(collection)

    def _read_slot(self, collection: str, path: Path) -> Slot | None:
        try:
            meta = json.loads((path / META_FILENAME).read_text(encoding="utf-8"))
            return Slot(
                name=path.name,
                collection=collection,
                product_id=meta["product_id"],
                sensing_start=_as_utc(datetime.fromisoformat(meta["sensing_start"])),
                sensing_end=_as_utc(datetime.fromisoformat(meta["sensing_end"])),
                path=path,
            )
        except (OSError, ValueError, KeyError):
            return None

    def slots(self, collection: str) -> list[Slot]:
        """Alle vollständig angelegten Slots, neuester zuerst."""
        directory = self.collection_dir(collection)
        if not directory.is_dir():
            return []
        slots = [
            slot
            for path in directory.iterdir()
            if path.is_dir() and _SLOT_RE.match(path.name)
            and (slot := self._read_slot(collection, path)) is not None
        ]
        return sorted(slots, key=lambda s: s.sensing_end, reverse=True)

    def slot(self, collection: str, name: str) -> Slot | None:
        if not _SLOT_RE.match(name):
            return None
        return self._read_slot(collection, self.collection_dir(collection) / name)

    def status(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._status.items()}

    # -- Download ----------------------------------------------------------
    def sync(
        self,
        collection: str,
        credentials: EumetsatCredentials,
        chunks: set[int],
        retention_hours: int,
    ) -> Slot:
        """Neuesten Slot sicherstellen (fehlende Chunks laden), alte entfernen."""
        status = self._status.setdefault(collection, {})
        status["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        try:
            slot = self._sync(collection, credentials, chunks)
        except ArchiveError as exc:
            status["last_error"] = str(exc)
            raise
        status["last_error"] = None
        status["newest"] = slot.name
        self.prune(collection, retention_hours)
        return slot

    def _sync(self, collection: str, credentials: EumetsatCredentials, chunks: set[int]) -> Slot:
        if not credentials.consumer_key or not credentials.consumer_secret:
            raise ArchiveError(
                "Keine EUMETSAT-Zugangsdaten hinterlegt (Web-UI oder Umgebungsvariablen)"
            )
        import eumdac  # teuer im Import, nur bei echtem Bedarf laden

        with self._lock:
            try:
                token = eumdac.AccessToken((credentials.consumer_key, credentials.consumer_secret))
                store_collection = eumdac.DataStore(token).get_collection(collection)
                now = datetime.now(timezone.utc)
                results = store_collection.search(
                    dtstart=(now - SEARCH_WINDOW).replace(tzinfo=None),
                    dtend=now.replace(tzinfo=None),
                )
                newest = next(iter(results), None)
            except Exception as exc:
                raise ArchiveError(f"Data-Store-Suche fehlgeschlagen: {exc}") from exc
            if newest is None:
                raise ArchiveError(f"Kein Produkt in {collection} innerhalb der letzten 2 Stunden")

            product_id = str(newest)
            sensing_end = _as_utc(newest.sensing_end)
            sensing_start = _as_utc(getattr(newest, "sensing_start", None) or sensing_end)
            slot_dir = self.collection_dir(collection) / sensing_end.strftime(SLOT_FORMAT)
            slot = Slot(slot_dir.name, collection, product_id, sensing_start, sensing_end, slot_dir)

            present = slot.chunk_files()
            entries = {}
            for entry in newest.entries:
                chunk = chunk_of(entry)
                if chunk in chunks and chunk not in present:
                    entries[chunk] = entry
            if entries:
                logger.info(
                    "FCI %s: lade Chunks %s (%s)",
                    slot.name, format_chunks(list(entries)), collection,
                )
            slot_dir.mkdir(parents=True, exist_ok=True)
            try:
                for chunk, entry in sorted(entries.items()):
                    target = slot_dir / entry.split("/")[-1]
                    partial = target.with_name(target.name + ".part")
                    try:
                        with newest.open(entry=entry) as source, partial.open("wb") as sink:
                            shutil.copyfileobj(source, sink, length=1024 * 1024)
                        partial.rename(target)
                    except Exception:
                        partial.unlink(missing_ok=True)
                        raise
            except Exception as exc:
                if not slot.chunk_files():
                    shutil.rmtree(slot_dir, ignore_errors=True)
                raise ArchiveError(f"Download von {product_id} fehlgeschlagen: {exc}") from exc

            meta = {
                "product_id": product_id,
                "sensing_start": sensing_start.isoformat(),
                "sensing_end": sensing_end.isoformat(),
            }
            tmp = slot_dir / (META_FILENAME + ".part")
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            tmp.rename(slot_dir / META_FILENAME)
            return slot

    # -- Aufräumen ---------------------------------------------------------
    def prune(self, collection: str, retention_hours: int) -> int:
        """Löscht Slots älter als die Aufbewahrungszeit; der neueste bleibt."""
        slots = self.slots(collection)
        if not slots:
            return 0
        cutoff = slots[0].sensing_end - timedelta(hours=retention_hours)
        removed = 0
        for slot in slots[1:]:
            if slot.sensing_end <= cutoff:
                shutil.rmtree(slot.path, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("FCI-Archiv %s: %d alte Slots entfernt", collection, removed)
        return removed

    def prune_all(self, retention_hours: int) -> int:
        """Alle Collections im Archiv aufräumen (auch nicht mehr konfigurierte)."""
        if not self.root.is_dir():
            return 0
        return sum(
            self.prune(directory.name.replace("_", ":"), retention_hours)
            for directory in self.root.iterdir()
            if directory.is_dir()
        )

    def collections(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(d.name.replace("_", ":") for d in self.root.iterdir() if d.is_dir())


def render_cache_path(slot: Slot, region: RegionConfig, composite: str) -> Path:
    """Cache-Datei für ein bei Bedarf gerendertes Bild (fällt mit dem Slot weg)."""
    geometry = f"{region.lat}|{region.lon}|{region.radius_km}|{region.width}|{region.height}"
    digest = hashlib.sha1(geometry.encode()).hexdigest()[:10]
    return slot.path / RENDERS_SUBDIR / f"{region.name}-{resolve_fci_composite(composite)}-{digest}.png"


_archives: dict[Path, FciArchive] = {}
_archives_lock = threading.Lock()


def get_archive(root: Path) -> FciArchive:
    root = Path(root)
    with _archives_lock:
        if root not in _archives:
            _archives[root] = FciArchive(root)
        return _archives[root]
