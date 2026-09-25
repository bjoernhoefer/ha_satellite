"""Thread-sicherer Status-Speicher (letzter/nächster Lauf, Fehler)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RegionStatus:
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    frame_count: int = 0

    def latest_age_seconds(self) -> float | None:
        if self.last_run_at is None:
            return None
        return (datetime.now(timezone.utc) - self.last_run_at).total_seconds()

    def as_dict(self) -> dict:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_error": self.last_error,
            "frame_count": self.frame_count,
            "latest_age_seconds": self.latest_age_seconds(),
        }


class StatusStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._regions: dict[str, RegionStatus] = {}

    def get(self, region: str) -> RegionStatus:
        with self._lock:
            return self._regions.setdefault(region, RegionStatus())

    def record_success(self, region: str, frame_count: int, next_run_at: datetime | None) -> None:
        with self._lock:
            status = self._regions.setdefault(region, RegionStatus())
            status.last_run_at = datetime.now(timezone.utc)
            status.next_run_at = next_run_at
            status.last_error = None
            status.frame_count = frame_count

    def record_error(self, region: str, error: str, next_run_at: datetime | None) -> None:
        with self._lock:
            status = self._regions.setdefault(region, RegionStatus())
            status.last_error = error
            status.next_run_at = next_run_at

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {name: status.as_dict() for name, status in self._regions.items()}
