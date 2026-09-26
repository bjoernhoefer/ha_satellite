"""Rollierender Ringpuffer für Frames pro Region.

Speichert PNG-Frames unter ``/data/frames/{region}/`` und pflegt einen
Index (``_index.json``) mit der zeitlichen Reihenfolge. Beim Hinzufügen
neuer Frames werden ältere Frames oberhalb der konfigurierten Anzahl
sowie oberhalb eines Speicher-Limits (in MB) automatisch entfernt.
Dateien auf der Platte, die nicht (mehr) im Index stehen ("Waisen"),
werden ebenfalls aufgeräumt.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

INDEX_FILENAME = "_index.json"

logger = logging.getLogger(__name__)


@dataclass
class Frame:
    filename: str
    created_at: str
    # Herkunft (Katalog-ID der Quelle + Komposit). Nach einem Quellenwechsel
    # zählt nur der neueste Frame derselben Herkunft als "bereits gerendert".
    source: str | None = None
    composite: str | None = None

    def as_dict(self) -> dict:
        data = {"filename": self.filename, "created_at": self.created_at}
        if self.source is not None:
            data["source"] = self.source
        if self.composite is not None:
            data["composite"] = self.composite
        return data

    def path(self, region_dir: Path) -> Path:
        return region_dir / self.filename


@dataclass
class RingBuffer:
    """Ringpuffer für eine einzelne Region."""

    region_dir: Path
    max_frames: int = 4
    max_storage_mb: float = 500
    _frames: list[Frame] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        self.region_dir = Path(self.region_dir)
        self.region_dir.mkdir(parents=True, exist_ok=True)
        self._frames = self._load_index()
        self._cleanup()

    # -- Persistenz -------------------------------------------------
    def _index_path(self) -> Path:
        return self.region_dir / INDEX_FILENAME

    def _load_index(self) -> list[Frame]:
        index_path = self._index_path()
        if not index_path.exists():
            return []
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        known = {"filename", "created_at", "source", "composite"}
        frames = [Frame(**{k: v for k, v in entry.items() if k in known}) for entry in raw]
        # Nur Frames behalten, deren Datei tatsächlich existiert.
        return [f for f in frames if f.path(self.region_dir).exists()]

    def _save_index(self) -> None:
        payload = [f.as_dict() for f in self._frames]
        self._index_path().write_text(json.dumps(payload), encoding="utf-8")

    # -- Öffentliche API ----------------------------------------------
    def add_frame(
        self,
        data: bytes,
        timestamp: datetime | None = None,
        source: str | None = None,
        composite: str | None = None,
    ) -> Frame:
        """Fügt einen neuen Frame (PNG-Bytes) als neuesten hinzu und räumt auf.

        Die Reihenfolge ist die des Hinzufügens: nach einem Wechsel auf eine
        Quelle mit älterer Aufnahme (z. B. Rapid Scan -> 0°) ist das neu
        gerenderte Bild trotzdem Frame 0.
        """
        with self._lock:
            ts = timestamp or datetime.now(timezone.utc)
            stem = ts.strftime('%Y%m%dT%H%M%S%fZ')
            filename = f"{stem}.png"
            counter = 1
            while (self.region_dir / filename).exists():
                filename = f"{stem}-{counter}.png"
                counter += 1
            (self.region_dir / filename).write_bytes(data)
            frame = Frame(
                filename=filename, created_at=ts.isoformat(), source=source, composite=composite
            )
            self._frames.append(frame)
            self._cleanup()
            self._save_index()
            return frame

    def frames_newest_first(self) -> list[Frame]:
        with self._lock:
            return list(reversed(self._frames))

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def get(self, index: int) -> Frame | None:
        """Liefert Frame mit Index 0 = neuester Frame, aufsteigend = älter."""
        with self._lock:
            frames = self.frames_newest_first()
            if 0 <= index < len(frames):
                return frames[index]
            return None

    def by_filename(self, filename: str) -> Frame | None:
        with self._lock:
            for frame in self._frames:
                if frame.filename == filename:
                    return frame
            return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    # -- Aufräumen ----------------------------------------------------
    def _cleanup(self) -> None:
        self._enforce_max_frames()
        self._enforce_storage_limit()
        self._remove_orphans()

    def _enforce_max_frames(self) -> None:
        while len(self._frames) > self.max_frames:
            oldest = self._frames.pop(0)
            self._delete_file(oldest.filename)

    def _enforce_storage_limit(self) -> None:
        limit_bytes = self.max_storage_mb * 1024 * 1024
        while self._frames and self._total_size() > limit_bytes:
            oldest = self._frames.pop(0)
            self._delete_file(oldest.filename)

    def _remove_orphans(self) -> None:
        known = {f.filename for f in self._frames}
        known.add(INDEX_FILENAME)
        for path in self.region_dir.iterdir():
            if path.is_file() and path.name not in known:
                path.unlink(missing_ok=True)

    def _total_size(self) -> int:
        total = 0
        for f in self._frames:
            path = f.path(self.region_dir)
            if path.exists():
                total += path.stat().st_size
        return total

    def _delete_file(self, filename: str) -> None:
        (self.region_dir / filename).unlink(missing_ok=True)

    def update_limits(self, max_frames: int, max_storage_mb: float) -> None:
        """Passt Max-Frames/Speicherlimit an und räumt danach direkt auf."""
        with self._lock:
            self.max_frames = max_frames
            self.max_storage_mb = max_storage_mb
            self._cleanup()
            self._save_index()


class BufferManager:
    """Verwaltet je einen RingBuffer pro Region."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self._buffers: dict[str, RingBuffer] = {}
        self._lock = threading.RLock()

    def get(self, region_name: str, max_frames: int, max_storage_mb: float) -> RingBuffer:
        with self._lock:
            buf = self._buffers.get(region_name)
            if buf is None:
                buf = RingBuffer(
                    region_dir=self.base_dir / region_name,
                    max_frames=max_frames,
                    max_storage_mb=max_storage_mb,
                )
                self._buffers[region_name] = buf
            else:
                buf.update_limits(max_frames, max_storage_mb)
            return buf

    def relocate(self, new_base_dir: Path, move_existing: bool) -> int:
        """Wechselt den Speicherort; verschiebt vorhandene Frames auf Wunsch.

        Existiert eine Region am Ziel bereits, werden die Frames zusammengeführt
        (Index nach Zeitstempel sortiert). Liefert die Zahl verschobener Frames.
        """
        new_base_dir = Path(new_base_dir)
        with self._lock:
            old_base_dir = self.base_dir
            moved = 0
            if move_existing and old_base_dir.exists() and old_base_dir.resolve() != new_base_dir.resolve():
                for region_dir in sorted(p for p in old_base_dir.iterdir() if p.is_dir()):
                    if region_dir.name.startswith("_"):
                        # Interne Verzeichnisse (z. B. FCI-Rohdaten-Archiv)
                        # haben keinen Frame-Index und werden als Ganzes verschoben.
                        _move_internal_dir(region_dir, new_base_dir / region_dir.name)
                        continue
                    moved += _merge_region_dir(region_dir, new_base_dir / region_dir.name)
            self.base_dir = new_base_dir
            self._buffers.clear()
            return moved


def _move_internal_dir(source: Path, target: Path) -> None:
    """Verschiebt Unterordner, die am Ziel noch fehlen; Rest wird verworfen."""
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and destination.exists():
            _move_internal_dir(child, destination)
        elif not destination.exists():
            shutil.move(str(child), str(destination))
    shutil.rmtree(source, ignore_errors=True)


def _merge_region_dir(source: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)

    def _read(directory: Path) -> list[dict]:
        try:
            return json.loads((directory / INDEX_FILENAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    merged = {entry["filename"]: entry for entry in _read(target)}
    moved = 0
    for entry in _read(source):
        src_file = source / entry["filename"]
        if not src_file.exists():
            continue
        if entry["filename"] not in merged:
            shutil.move(str(src_file), str(target / entry["filename"]))
            merged[entry["filename"]] = entry
            moved += 1
    # Ohne vorhandene Frames am Ziel bleibt die Reihenfolge des Hinzufügens
    # erhalten (sie weicht nach Quellenwechseln von der Zeitreihenfolge ab).
    entries = list(merged.values())
    if len(entries) != moved:
        entries.sort(key=lambda e: e["created_at"])
    (target / INDEX_FILENAME).write_text(json.dumps(entries), encoding="utf-8")
    shutil.rmtree(source, ignore_errors=True)
    logger.info("%d Frames von %s nach %s verschoben", moved, source, target)
    return moved
