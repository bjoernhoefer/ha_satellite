"""Abgeleitete Medien je Region, auf der Platte gecacht.

JPEG-Vorschaubilder (``?w=240``), JPEG in voller Größe (MJPEG-Stream) und
die Animationen (GIF/MP4) werden einmal erzeugt und unter
``<Region>/_cache/`` abgelegt. Der Scheduler erzeugt sie direkt nach jedem
neuen Frame vorab (``prewarm``), sodass Betrachter, Zeitraffer und
Home Assistant nicht auf die Umrechnung warten. Der Name einer Animation
enthält einen Hash über die Frame-Liste und die Bildrate: jede Änderung
ergibt eine neue Datei, die alte wird entfernt. Vorschaubilder gelöschter
Frames räumt der Ringpuffer auf (``RingBuffer._remove_orphans``).
"""

from __future__ import annotations

import hashlib
import io
import logging
import tempfile
import threading
from pathlib import Path

from PIL import Image

from ha_satellite.buffer import CACHE_DIRNAME, Frame, RingBuffer

logger = logging.getLogger(__name__)

THUMB_WIDTH = 240
ANIMATION_PREFIX = "animation-"
# Das GIF hält alle Frames im Speicher (PIL); bei langer Historie nicht
# automatisch erzeugen (768-MB-Limit auf dem Pi), nur auf Anfrage.
GIF_PREWARM_MAX_FRAMES = 60

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    partial.rename(path)


def cache_dir(buffer: RingBuffer) -> Path:
    return buffer.region_dir / CACHE_DIRNAME


# -- Einzelbilder ------------------------------------------------------------
def jpeg_path(buffer: RingBuffer, frame: Frame, width: int | None = None) -> Path:
    suffix = f"w{width}" if width else "full"
    return cache_dir(buffer) / f"{Path(frame.filename).stem}-{suffix}.jpg"


def _to_jpeg(png_bytes: bytes, width: int | None) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        if width:
            img.thumbnail((width, width * 4))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80 if width else 90)
        return out.getvalue()


def jpeg(buffer: RingBuffer, frame: Frame, width: int | None = None) -> bytes:
    """JPEG eines Frames (optional verkleinert), gecacht."""
    path = jpeg_path(buffer, frame, width)
    if path.exists():
        return path.read_bytes()
    with _lock_for(path):
        if path.exists():
            return path.read_bytes()
        data = _to_jpeg(frame.path(buffer.region_dir).read_bytes(), width)
        if buffer.by_filename(frame.filename) is not None:  # nicht inzwischen gelöscht
            _write_atomic(path, data)
        return data


# -- Animationen -------------------------------------------------------------
def _animation_path(buffer: RingBuffer, frames: list[Frame], fps: float, ext: str) -> Path:
    key = "|".join(f.filename for f in frames) + f"|{fps}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    return cache_dir(buffer) / f"{ANIMATION_PREFIX}{digest}.{ext}"


def _iter_images(buffer: RingBuffer, frames: list[Frame]):
    for frame in frames:
        with Image.open(frame.path(buffer.region_dir)) as img:
            yield img.convert("RGB")


def _encode_gif(images, fps: float) -> bytes:
    images = list(images)
    out = io.BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=max(int(1000 / fps), 50),
        loop=0,
    )
    return out.getvalue()


def _encode_mp4(images, fps: float) -> bytes:
    import imageio.v2 as imageio  # ImportError -> Aufrufer meldet 501
    import numpy as np

    # Das FFMPEG-Plugin von imageio schreibt nur in echte Dateien, nicht in
    # BytesIO - daher der Umweg über eine temporäre Datei.
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "animation.mp4"
        with imageio.get_writer(
            target,
            format="FFMPEG",
            mode="I",
            fps=fps,
            codec="libx264",
            pixelformat="yuv420p",
            macro_block_size=None,
        ) as writer:
            for image in images:
                writer.append_data(np.asarray(image))
        return target.read_bytes()


ENCODERS = {"gif": _encode_gif, "mp4": _encode_mp4}


def animation(buffer: RingBuffer, ext: str, fps: float) -> bytes | None:
    """Animation der Historie (älteste zuerst), gecacht; ``None`` ohne Frames."""
    frames = list(reversed(buffer.frames_newest_first()))
    if not frames:
        return None
    path = _animation_path(buffer, frames, fps, ext)
    if path.exists():
        return path.read_bytes()
    with _lock_for(path):
        if path.exists():
            return path.read_bytes()
        data = ENCODERS[ext](_iter_images(buffer, frames), fps)
        _write_atomic(path, data)
        for old in cache_dir(buffer).glob(f"{ANIMATION_PREFIX}*.{ext}"):
            if old != path:
                old.unlink(missing_ok=True)
        logger.info(
            "Animation %s für %s erzeugt (%d Frames, %d KB)",
            ext.upper(), buffer.region_dir.name, len(frames), len(data) // 1024,
        )
        return data


def prewarm(buffer: RingBuffer, fps: float) -> None:
    """Nach einem neuen Frame: Vorschaubild, JPEG und Animationen erzeugen."""
    latest = buffer.latest()
    if latest is None:
        return
    for width in (THUMB_WIDTH, None):
        jpeg(buffer, latest, width)
    if len(buffer) <= GIF_PREWARM_MAX_FRAMES:
        animation(buffer, "gif", fps)
    try:
        animation(buffer, "mp4", fps)
    except ImportError:
        logger.debug("MP4 übersprungen: imageio-ffmpeg fehlt")
