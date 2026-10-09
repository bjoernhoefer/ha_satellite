"""Frame storage location: candidates, checks, warnings.

Inside the container only paths mounted as volumes are persistent.
``docker-compose.yml`` therefore mounts the host's ``/mnt`` at ``/mnt`` -
so e.g. ``/mnt/data`` or ``/mnt/data2`` can be picked directly in the UI.
For each path we check whether it is writable, how much space is free and
whether it lives on the same disk as ``/data`` (usually the host's system
disk) or only in the container filesystem (lost when the container is
recreated).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

STORAGE_ROOTS_ENV = "HA_SATIMAGE_STORAGE_ROOTS"
DEFAULT_STORAGE_ROOTS = "/mnt:/media"
SUBDIR = "ha_satimage"


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
    """Describe a (possibly not yet existing) path."""
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
            f"Same disk as {data_dir} (usually the host's system disk)"
        )
    elif device is not None and device == root_device and not in_data_dir:
        info["container_only"] = True
        info["warnings"].append(
            "Only in the container filesystem - not mounted as a volume, "
            "images are lost when the container is recreated"
        )
    if not info["writable"]:
        info["warnings"].append(
            f"Not writable for UID {os.getuid()} / GID {os.getgid()} "
            "(check permissions on the host or PUID/PGID)"
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
    """Create the directory and verify with a test file that writing works."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise StorageError(
            f"Storage location {path} is not writable ({exc.strerror or exc}). "
            f"The service runs as UID {os.getuid()} / GID {os.getgid()} - create the directory "
            "on the host and adjust permissions, or set PUID/PGID in Compose."
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
