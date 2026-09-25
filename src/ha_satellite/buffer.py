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
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

INDEX_FILENAME = "_index.json"


@dataclass
class Frame:
    filename: str
    created_at: str

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
        frames = [Frame(**entry) for entry in raw]
        # Nur Frames behalten, deren Datei tatsächlich existiert.
        return [f for f in frames if f.path(self.region_dir).exists()]

    def _save_index(self) -> None:
        payload = [{"filename": f.filename, "created_at": f.created_at} for f in self._frames]
        self._index_path().write_text(json.dumps(payload), encoding="utf-8")

    # -- Öffentliche API ----------------------------------------------
    def add_frame(self, data: bytes, timestamp: datetime | None = None) -> Frame:
        """Fügt einen neuen Frame (PNG-Bytes) hinzu und räumt danach auf."""
        with self._lock:
            ts = timestamp or datetime.now(timezone.utc)
            filename = f"{ts.strftime('%Y%m%dT%H%M%S%fZ')}.png"
            (self.region_dir / filename).write_bytes(data)
            frame = Frame(filename=filename, created_at=ts.isoformat())
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
