"""Speicherort der Frames: Kandidaten, Prüfung, Warnungen.

Im Container sind nur Pfade persistent, die als Volume eingehängt sind.
``docker-compose.yml`` hängt dafür ``/mnt`` des Hosts unter ``/mnt`` ein -
so lassen sich z. B. ``/mnt/data`` oder ``/mnt/data2`` direkt in der UI
auswählen. Für jeden Pfad wird geprüft, ob er beschreibbar ist, wie viel
Platz frei ist und ob er auf demselben Datenträger wie ``/data`` (in der
Regel die System-Disk des Hosts) oder gar nur im Container-Dateisystem
liegt (geht bei Neuerstellung des Containers verloren).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

STORAGE_ROOTS_ENV = "HA_SATELLITE_STORAGE_ROOTS"
DEFAULT_STORAGE_ROOTS = "/mnt:/media"
SUBDIR = "ha_satellite"


class StorageError(Exception):
    pass


def storage_roots() -> list[Path]:
    raw = os.environ.get(STORAGE_ROOTS_ENV, DEFAULT_STORAGE_ROOTS)
    return [Path(p) for p in raw.split(":") if p.strip()]


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _device(path: Path) -> int | None:
    try:
        return os.stat(_existing_ancestor(path)).st_dev
    except OSError:
        return None


def _is_writable(path: Path) -> bool:
    target = _existing_ancestor(path)
    return os.access(target, os.W_OK | os.X_OK)


def describe(path: Path, data_dir: Path) -> dict:
    """Beschreibt einen (möglicherweise noch nicht existierenden) Pfad."""
    anchor = _existing_ancestor(path)
    info: dict = {
        "path": str(path),
        "exists": path.exists(),
        "writable": _is_writable(path),
        "free_bytes": None,
        "total_bytes": None,
        "same_disk_as_data": False,
        "container_only": False,
        "warnings": [],
    }
    try:
        usage = shutil.disk_usage(anchor)
        info["free_bytes"] = usage.free
        info["total_bytes"] = usage.total
    except OSError:
        pass
    device = _device(path)
    root_device = _device(Path("/"))
    data_device = _device(data_dir)
    in_data_dir = path == data_dir or data_dir in path.parents
    if device is not None and device == data_device:
        info["same_disk_as_data"] = True
        info["warnings"].append(
            f"Gleicher Datenträger wie {data_dir} (in der Regel die System-Disk des Hosts)"
        )
    elif device is not None and device == root_device and not in_data_dir:
        info["container_only"] = True
        info["warnings"].append(
            "Liegt nur im Container-Dateisystem - nicht als Volume eingehängt, "
            "Bilder gehen beim Neuerstellen des Containers verloren"
        )
    if not info["writable"]:
        info["warnings"].append(
            f"Nicht beschreibbar für UID {os.getuid()} / GID {os.getgid()} "
            "(Rechte auf dem Host bzw. PUID/PGID prüfen)"
        )
    return info


def candidates(default_dir: Path, current_dir: Path, data_dir: Path) -> list[dict]:
    paths: list[Path] = [default_dir]
    for root in storage_roots():
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        paths.extend(child / SUBDIR for child in children)
    if current_dir not in paths:
        paths.insert(0, current_dir)
    result = []
    for path in paths:
        info = describe(path, data_dir)
        info["is_default"] = path == default_dir
        info["is_current"] = path == current_dir
        result.append(info)
    return result


def ensure_writable(path: Path) -> None:
    """Legt das Verzeichnis an und prüft per Testdatei, ob Schreiben klappt."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise StorageError(
            f"Speicherort {path} ist nicht beschreibbar ({exc.strerror or exc}). "
            f"Der Dienst läuft als UID {os.getuid()} / GID {os.getgid()} - Verzeichnis auf "
            "dem Host anlegen und Rechte anpassen bzw. PUID/PGID im Compose setzen."
        ) from exc


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total
