"""Archive of pre-rendered images: every source in every image type.

While the ring buffer (``buffer.py``) keeps the *configured* image type of
a region, the archive keeps **all combinations**: for every region, every
enabled source and every image type (composite) of that source's driver one
image per capture. They are rendered right after a source delivered a new
capture ("download all sources"), so the archive viewer is as fast as the
normal viewer - nothing is rendered while browsing.

Layout (below the frame storage location)::

    _renders/<region>/<source>/<composite>/<YYYYmmddTHHMMSSZ>.png
                                          /<YYYYmmddTHHMMSSZ>.jpg

The slot name is the capture time (end of scan), so the same capture is
rendered only once per combination. Cleanup is by age
(``archive.render_retention_hours``) and by a storage limit
(``archive.render_max_storage_mb``, oldest first).
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satellite.overlay import OVERLAY_VERSION

logger = logging.getLogger(__name__)

RENDERS_SUBDIR = "_renders"
SLOT_FORMAT = "%Y%m%dT%H%M%SZ"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SLOT_RE = re.compile(r"^\d{8}T\d{6}Z$")


def slot_name(sensing: datetime) -> str:
    if sensing.tzinfo is None:
        sensing = sensing.replace(tzinfo=timezone.utc)
    return sensing.astimezone(timezone.utc).strftime(SLOT_FORMAT)


def slot_time(name: str) -> datetime | None:
    if not _SLOT_RE.match(name):
        return None
    try:
        return datetime.strptime(name, SLOT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _valid(segment: str) -> bool:
    return bool(_SEGMENT_RE.match(segment)) and segment not in (".", "..")


def region_signature(region) -> str:
    """Cut-out of a region; a change invalidates its archived images."""
    return "|".join(
        str(v)
        for v in (
            region.lat, region.lon, region.radius_km,
            region.width, region.height, region.borders, OVERLAY_VERSION,
        )
    )


@dataclass(frozen=True)
class ArchivedImage:
    region: str
    source: str
    composite: str
    slot: str
    created_at: datetime
    path: Path

    @property
    def jpeg_path(self) -> Path:
        return self.path.with_suffix(".jpg")

    def as_dict(self, region_name: str) -> dict:
        base = (
            f"/regions/{region_name}/archive/{self.source}/{self.composite}/{self.slot}"
        )
        version = self.path.stat().st_mtime_ns
        jpeg_version = self.jpeg_path.stat().st_mtime_ns if self.jpeg_path.exists() else version
        return {
            "name": self.slot,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "composite": self.composite,
            "available": True,
            "cached": True,
            "url": f"{base}.png?v={version}",
            "jpeg_url": f"{base}.jpg?v={jpeg_version}",
        }


class RenderArchive:
    """Stores and serves pre-rendered images per region/source/composite."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    # -- Paths -------------------------------------------------------------
    def dir_for(self, region: str, source: str, composite: str) -> Path | None:
        if not all(_valid(part) for part in (region, source, composite)):
            return None
        return self.root / region / source / composite

    def path_for(
        self, region: str, source: str, composite: str, slot: str, ext: str = "png"
    ) -> Path | None:
        directory = self.dir_for(region, source, composite)
        if directory is None or not _SLOT_RE.match(slot) or ext not in ("png", "jpg"):
            return None
        return directory / f"{slot}.{ext}"

    def has(self, region: str, source: str, composite: str, sensing: datetime) -> bool:
        path = self.path_for(region, source, composite, slot_name(sensing))
        return path is not None and path.exists()

    # -- Writing -----------------------------------------------------------
    def store(
        self,
        region: str,
        source: str,
        composite: str,
        sensing: datetime,
        png: bytes,
    ) -> ArchivedImage | None:
        """Write PNG (plus a JPEG for fast browsing); ``None`` on invalid names."""
        slot = slot_name(sensing)
        path = self.path_for(region, source, composite, slot)
        if path is None:
            logger.warning(
                "Archive: invalid name %s/%s/%s - not stored", region, source, composite
            )
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(path, png)
        try:
            from ha_satellite.media import to_jpeg

            self._write_atomic(path.with_suffix(".jpg"), to_jpeg(png, None))
        except Exception:  # pragma: no cover - defensive, PNG stays usable
            logger.exception("Archive: could not create the JPEG for %s", path.name)
        return ArchivedImage(
            region, source, composite, slot, slot_time(slot) or sensing, path
        )

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        partial = path.with_name(path.name + ".part")
        partial.write_bytes(data)
        partial.rename(path)

    # -- Reading -----------------------------------------------------------
    def images(self, region: str, source: str, composite: str) -> list[ArchivedImage]:
        """All archived images of the combination, newest first."""
        directory = self.dir_for(region, source, composite)
        if directory is None or not directory.is_dir():
            return []
        images = []
        for path in directory.iterdir():
            if path.suffix != ".png":
                continue
            created = slot_time(path.stem)
            if created is not None:
                images.append(
                    ArchivedImage(region, source, composite, path.stem, created, path)
                )
        return sorted(images, key=lambda i: i.created_at, reverse=True)

    def combinations(self, region: str) -> dict[str, list[str]]:
        """Source ID -> image types with archived images (for the UI)."""
        if not _valid(region):
            return {}
        base = self.root / region
        if not base.is_dir():
            return {}
        result: dict[str, list[str]] = {}
        for source_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            composites = sorted(
                p.name
                for p in source_dir.iterdir()
                if p.is_dir() and any(f.suffix == ".png" for f in p.iterdir())
            )
            if composites:
                result[source_dir.name] = composites
        return result

    def regions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    # -- Region signature ---------------------------------------------------
    def sync_region(self, region_name: str, signature: str) -> bool:
        """Update overlays in place; drop images only for incompatible cut-outs.

        Geometry changes and removing baked-in borders invalidate the archive.
        Adding borders must not delete historical scans: SEVIRI raw products
        are not retained. Legacy five-field signatures predate borders.
        Returns ``True`` when the signature changed.
        """
        if not _valid(region_name):
            return False
        with self._lock:
            base = self.root / region_name
            marker = base / "region.json"
            try:
                current = marker.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == signature:
                return False
            old = current.split("|") if current else []
            new = signature.split("|")
            same_geometry = len(old) in (5, 6, 7) and old[:5] == new[:5]
            had_borders = len(old) >= 6 and old[5] == "True"
            has_borders = len(new) >= 6 and new[5] == "True"
            if same_geometry and (has_borders or not had_borders):
                if has_borders:
                    self._update_overlays(region_name, signature, had_borders)
            elif current is not None or (base.is_dir() and any(p.is_dir() for p in base.iterdir())):
                shutil.rmtree(base, ignore_errors=True)
                logger.info("Archive %s: cut-out changed - archived images removed", region_name)
            base.mkdir(parents=True, exist_ok=True)
            self._write_atomic(marker, signature.encode("utf-8"))
            return True

    def _update_overlays(self, region: str, signature: str, had_borders: bool) -> None:
        from PIL import Image, PngImagePlugin

        from ha_satellite.media import to_jpeg
        from ha_satellite.overlay import draw_borders, draw_coastlines

        lat, lon, radius = map(float, signature.split("|")[:3])
        draw = draw_coastlines if had_borders else draw_borders
        updated = 0
        for source, composites in self.combinations(region).items():
            for composite in composites:
                for archived in self.images(region, source, composite):
                    with Image.open(archived.path) as original:
                        # A retry after an interrupted migration must not darken
                        # already updated anti-aliased lines again.
                        if original.info.get("archive_overlay") == signature:
                            self._write_atomic(
                                archived.jpeg_path, to_jpeg(archived.path.read_bytes(), None)
                            )
                            continue
                        image = original.convert("RGB")
                    draw(image, lat, lon, radius)
                    metadata = PngImagePlugin.PngInfo()
                    metadata.add_text("archive_overlay", signature)
                    png = io.BytesIO()
                    image.save(png, format="PNG", pnginfo=metadata)
                    data = png.getvalue()
                    self._write_atomic(archived.path, data)
                    # Keep the region marker unchanged if either write fails,
                    # so an interrupted upgrade resumes on the next run.
                    self._write_atomic(archived.jpeg_path, to_jpeg(data, None))
                    updated += 1
        logger.info("Archive %s: updated borders on %d historical images", region, updated)

    def used_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def count(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(1 for p in self.root.rglob("*.png"))

    def summary(self) -> dict:
        regions = {}
        for region in self.regions():
            images = [
                image
                for source, composites in self.combinations(region).items()
                for composite in composites
                for image in self.images(region, source, composite)
            ]
            if not images:
                continue
            regions[region] = {
                "images": len(images),
                "combinations": sum(
                    len(c) for c in self.combinations(region).values()
                ),
                "newest": max(i.created_at for i in images).isoformat(),
                "oldest": min(i.created_at for i in images).isoformat(),
            }
        return {
            "dir": str(self.root),
            "images": self.count(),
            "used_bytes": self.used_bytes(),
            "regions": regions,
        }

    # -- Cleanup -----------------------------------------------------------
    def _all_images(self) -> list[ArchivedImage]:
        return [
            image
            for region in self.regions()
            for source, composites in self.combinations(region).items()
            for composite in composites
            for image in self.images(region, source, composite)
        ]

    def _remove(self, image: ArchivedImage) -> None:
        image.path.unlink(missing_ok=True)
        image.jpeg_path.unlink(missing_ok=True)

    def prune(self, retention_hours: int, max_storage_mb: float) -> int:
        """Remove images older than the retention time and beyond the limit."""
        with self._lock:
            images = self._all_images()
            if not images:
                return 0
            removed = 0
            newest = max(i.created_at for i in images)
            cutoff = newest - timedelta(hours=max(retention_hours, 0))
            keep: list[ArchivedImage] = []
            for image in images:
                if image.created_at < cutoff:
                    self._remove(image)
                    removed += 1
                else:
                    keep.append(image)
            limit = max_storage_mb * 1024 * 1024
            keep.sort(key=lambda i: i.created_at)
            total = sum(self._size(i) for i in keep)
            while keep and total > limit:
                oldest = keep.pop(0)
                total -= self._size(oldest)
                self._remove(oldest)
                removed += 1
            self._drop_empty_dirs()
            if removed:
                logger.info("Archive: removed %d rendered images", removed)
            return removed

    def forget(self, regions: set[str]) -> int:
        """Remove archive directories of regions that no longer exist."""
        removed = 0
        for region in self.regions():
            if region not in regions:
                shutil.rmtree(self.root / region, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("Archive: removed %d directories of deleted regions", removed)
        return removed

    @staticmethod
    def _size(image: ArchivedImage) -> int:
        total = 0
        for path in (image.path, image.jpeg_path):
            if path.exists():
                total += path.stat().st_size
        return total

    def _drop_empty_dirs(self) -> None:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*"), key=lambda p: -len(p.parts)):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


_archives: dict[Path, RenderArchive] = {}
_archives_lock = threading.Lock()


def get_render_archive(root: Path) -> RenderArchive:
    root = Path(root)
    with _archives_lock:
        if root not in _archives:
            _archives[root] = RenderArchive(root)
        return _archives[root]
