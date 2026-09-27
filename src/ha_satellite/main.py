"""FastAPI-Anwendung: Konfigurations-UI, REST-API und Bild-Endpunkte."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from ha_satellite import logbuffer, media, storage
from ha_satellite.buffer import BufferManager
from ha_satellite.config import (
    COMPOSITES,
    FCI_COMPOSITES,
    PLACEHOLDER_DRIVERS,
    VALID_DRIVERS,
    AppConfig,
    ConfigStore,
    EumetsatCredentials,
    composites_for,
    default_composite_for,
    env_overrides,
    resolve_fci_composite,
)
from ha_satellite.scheduler import RenderScheduler
from ha_satellite.sources import (
    RenderError,
    archive_root,
    fci_source_for,
    render_fci_slot,
)
from ha_satellite.sources.fci_archive import (
    chunks_for_region,
    format_chunks,
    get_archive,
    render_cache_path,
    wanted_chunks,
)
from ha_satellite.source_sync import SourceSync
from ha_satellite.status import StatusStore

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HA_SATELLITE_DATA_DIR", "/data"))
DEFAULT_FRAMES_DIR = DATA_DIR / "frames"

log_buffer = logbuffer.install(log_dir=DATA_DIR / "logs")
logger = logging.getLogger(__name__)


def frames_dir_for(config: AppConfig) -> Path:
    return Path(config.storage.frames_dir) if config.storage.frames_dir else DEFAULT_FRAMES_DIR


config_store = ConfigStore()
buffer_manager = BufferManager(frames_dir_for(config_store.get()))
status_store = StatusStore()
source_sync = SourceSync(config_store, DATA_DIR / "source_sync.json")
scheduler = RenderScheduler(config_store, buffer_manager, status_store, source_sync)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "ha_satellite startet: Daten in %s, Frames in %s (UID %d / GID %d)",
        DATA_DIR, buffer_manager.base_dir, os.getuid(), os.getgid(),
    )
    try:
        storage.ensure_writable(buffer_manager.base_dir)
    except storage.StorageError as exc:
        logger.error("%s", exc)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="ha_satellite", lifespan=lifespan)


def _get_region_or_404(config: AppConfig, region_name: str):
    region = config.region(region_name)
    if region is None:
        raise HTTPException(status_code=404, detail=f"Unbekannte Region: {region_name}")
    return region


def _buffer_for(region_name: str):
    config = config_store.get()
    region = _get_region_or_404(config, region_name)
    return buffer_manager.get(region.name, config.max_frames_for(region), config.history.max_storage_mb)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = config_store.get()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": config,
            "masked": config.masked(),
            "env_overrides": env_overrides().keys(),
            "status": status_store.all(),
            "drivers": VALID_DRIVERS,
            "composites": COMPOSITES,
            "composites_by_driver": {d: composites_for(d) for d in VALID_DRIVERS},
            "default_composites": {d: default_composite_for(d) for d in VALID_DRIVERS},
            "driver_of": {e.id: e.driver for e in config.sources.catalog},
            "fci_composites": FCI_COMPOSITES,
            "placeholder_drivers": PLACEHOLDER_DRIVERS,
        },
    )


@app.get("/live/{region_name}")
async def live_redirect(region_name: str):
    """Merkbare Adresse für den Live-Stream im Browser."""
    _get_region_or_404(config_store.get(), region_name)
    return RedirectResponse(url=f"/#live={region_name}")


@app.get("/api/config")
async def get_config():
    return JSONResponse(config_store.get().masked().model_dump())


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Rekursives Merge: verschachtelte Dicts werden Feld für Feld
    zusammengeführt statt komplett ersetzt (z. B. ``eumetsat`` oder
    einzelne ``sources``-Felder), Listen (z. B. ``regions``) werden als
    Ganzes übernommen, sofern angegeben."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_masked_echo(value: object, stored_secret: str) -> bool:
    """Erkennt, ob ein Client das maskierte Secret unverändert zurückschickt."""
    if not isinstance(value, str) or not value:
        return False
    return value == EumetsatCredentials(consumer_secret=stored_secret).masked().consumer_secret


@app.post("/api/config")
async def post_config(payload: dict):
    stored = config_store.stored()
    credentials = payload.get("eumetsat")
    if isinstance(credentials, dict):
        credentials = dict(credentials)
        secret = credentials.get("consumer_secret")
        # Leeres oder maskiertes Secret heißt "unverändert lassen" - sonst
        # würde jedes Speichern ohne Neueingabe das echte Secret zerstören.
        if secret is None or secret == "" or _is_masked_echo(secret, stored.eumetsat.consumer_secret):
            credentials.pop("consumer_secret", None)
        payload = {**payload, "eumetsat": credentials}
    merged = _deep_merge(stored.model_dump(), payload)
    try:
        new_config = AppConfig(**merged)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _save_config(stored, new_config, move_existing=False)
    changed = sorted(key for key in payload if key in AppConfig.model_fields)
    logger.info("Konfiguration gespeichert (%s)", ", ".join(changed) or "keine Änderung")
    return JSONResponse(config_store.get().masked().model_dump())


async def _save_config(old: AppConfig, new: AppConfig, move_existing: bool) -> int:
    """Speichert die Konfiguration; wechselt bei Bedarf den Speicherort."""
    old_dir, new_dir = frames_dir_for(old), frames_dir_for(new)
    moved = 0
    if old_dir != new_dir or buffer_manager.base_dir != new_dir:
        def _relocate() -> int:
            storage.ensure_writable(new_dir)
            with scheduler.exclusive():
                return buffer_manager.relocate(new_dir, move_existing)

        try:
            moved = await run_in_threadpool(_relocate)
        except (storage.StorageError, TimeoutError, OSError) as exc:
            logger.error("Speicherort-Wechsel nach %s fehlgeschlagen: %s", new_dir, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info(
            "Speicherort gewechselt: %s -> %s (%d Frames verschoben)", old_dir, new_dir, moved
        )
    config_store.update(new)
    scheduler.reload()
    return moved


@app.get("/api/storage")
async def get_storage():
    config = config_store.get()
    current = frames_dir_for(config)

    def _collect() -> dict:
        return {
            "current": str(current),
            "default": str(DEFAULT_FRAMES_DIR),
            "configured": config.storage.frames_dir,
            "used_bytes": storage.directory_size(current),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "candidates": storage.candidates(DEFAULT_FRAMES_DIR, current, DATA_DIR),
            "history_minutes": config.history.history_minutes,
            "max_storage_mb": config.history.max_storage_mb,
            "archive": _archive_summary(config),
        }

    return JSONResponse(await run_in_threadpool(_collect))


@app.post("/api/storage")
async def post_storage(payload: dict):
    """Speicherort (und Historien-Limits) setzen, Frames optional verschieben."""
    stored = config_store.stored()
    data = stored.model_dump()
    frames_dir = str(payload.get("frames_dir", data["storage"]["frames_dir"]) or "").strip()
    if frames_dir and Path(frames_dir) == DEFAULT_FRAMES_DIR:
        frames_dir = ""
    data["storage"]["frames_dir"] = frames_dir
    for key in ("history_minutes", "max_storage_mb"):
        if payload.get(key) is not None:
            data["history"][key] = payload[key]
    for key in ("retention_hours", "chunk_min", "chunk_max"):
        if payload.get(f"archive_{key}") is not None:
            data["archive"][key] = payload[f"archive_{key}"]
    try:
        new_config = AppConfig(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    moved = await _save_config(stored, new_config, move_existing=bool(payload.get("move_existing", True)))
    # Kürzere Aufbewahrung wirkt sofort, nicht erst beim nächsten Download.
    await run_in_threadpool(
        get_archive(archive_root(new_config)).prune_all, new_config.archive.retention_hours
    )
    return {"current": str(frames_dir_for(new_config)), "moved_frames": moved}


def _archive_summary(config: AppConfig) -> dict:
    archive = get_archive(archive_root(config))
    collections = {}
    for collection in archive.collections():
        slots = archive.slots(collection)
        collections[collection] = {
            "slots": len(slots),
            "newest": slots[0].sensing_end.isoformat() if slots else None,
            "oldest": slots[-1].sensing_end.isoformat() if slots else None,
            "used_bytes": storage.directory_size(archive.collection_dir(collection)),
        }
    chunks = wanted_chunks(config)
    return {
        "dir": str(archive.root),
        "retention_hours": config.archive.retention_hours,
        "chunk_min": config.archive.chunk_min,
        "chunk_max": config.archive.chunk_max,
        "chunks": sorted(chunks),
        "chunks_text": format_chunks(chunks),
        "enabled_sources": [
            e.id for e in config.sources.catalog if e.driver == "mtg_fci" and e.enabled
        ],
        "collections": collections,
        "status": archive.status(),
    }


@app.get("/api/archive")
async def get_archive_info():
    """FCI-Rohdaten-Archiv: Aufbewahrung, Chunks, Slots und Belegung."""
    return JSONResponse(await run_in_threadpool(_archive_summary, config_store.get()))


_COMPOSITE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _archive_context(region_name: str):
    config = config_store.get()
    region = _get_region_or_404(config, region_name)
    entry = fci_source_for(config, region)
    if entry is None:
        raise HTTPException(status_code=404, detail="Keine MTG-FCI-Quelle im Katalog")
    archive = get_archive(archive_root(config))
    return config, region, entry, archive


def _default_archive_composite(config: AppConfig, region) -> str:
    own = config.sources.get(region.source)
    if own is not None and own.driver == "mtg_fci":
        return resolve_fci_composite(region.composite)
    return default_composite_for("mtg_fci")


@app.get("/api/regions/{region_name}/archive")
async def list_archive(region_name: str, composite: str | None = None):
    """Archivierte FCI-Slots, aus denen die Region gerendert werden kann."""
    config, region, entry, archive = _archive_context(region_name)
    composite = composite or _default_archive_composite(config, region)
    if not _COMPOSITE_RE.match(composite):
        raise HTTPException(status_code=400, detail="Ungültiger Kompositname")
    needed = chunks_for_region(region)

    def _collect() -> list[dict]:
        items = []
        for slot in archive.slots(entry.collection or ""):
            available = bool(needed) and needed <= set(slot.chunks)
            items.append({
                "name": slot.name,
                "created_at": slot.sensing_end.isoformat(),
                "available": available,
                "cached": render_cache_path(slot, region, composite).exists(),
                "url": f"/regions/{region_name}/archive/{slot.name}.png?composite={composite}",
            })
        return items

    return {
        "region": region_name,
        "source": entry.id,
        "collection": entry.collection,
        "composite": composite,
        "composites": FCI_COMPOSITES,
        "chunks": sorted(needed),
        "retention_hours": config.archive.retention_hours,
        "slots": await run_in_threadpool(_collect),
    }


# Wartezeit auf den Render-Lock (ein planmäßiger Lauf inkl. Download).
ARCHIVE_RENDER_LOCK_TIMEOUT = 600


@app.get("/regions/{region_name}/archive/{slot_name}.png")
async def archive_png(region_name: str, slot_name: str, composite: str | None = None):
    """Rendert eine Region aus einem archivierten FCI-Slot (mit Cache)."""
    config, region, entry, archive = _archive_context(region_name)
    composite = composite or _default_archive_composite(config, region)
    if not _COMPOSITE_RE.match(composite):
        raise HTTPException(status_code=400, detail="Ungültiger Kompositname")
    slot = archive.slot(entry.collection or "", slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot nicht (mehr) im Archiv")
    cache = render_cache_path(slot, region, composite)

    def _render() -> bytes:
        if cache.exists():
            return cache.read_bytes()
        with scheduler.exclusive(timeout=ARCHIVE_RENDER_LOCK_TIMEOUT):
            if cache.exists():  # während des Wartens von einer anderen Anfrage erzeugt
                return cache.read_bytes()
            logger.info("Archiv-Render %s / %s / %s gestartet", region.name, slot.name, composite)
            frame = render_fci_slot(slot, region, composite)
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_name(cache.name + ".part")
        partial.write_bytes(frame.png)
        partial.rename(cache)
        return frame.png

    try:
        png = await run_in_threadpool(_render)
    except RenderError as exc:
        logger.error("Archiv-Render %s / %s fehlgeschlagen: %s", region.name, slot_name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Slot wurde inzwischen entfernt") from exc
    return Response(
        content=png, media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/sources")
async def get_sources():
    config = config_store.get()
    return {
        "catalog": [entry.model_dump() for entry in config.sources.catalog],
        "auto_sync_hours": config.sources.auto_sync_hours,
        "sync": source_sync.state(),
        "downloads": scheduler.download_status(),
    }


@app.post("/api/sources/sync")
async def sync_sources():
    return JSONResponse(await run_in_threadpool(source_sync.run))


@app.post("/api/sources/adopt")
async def adopt_source(payload: dict):
    collection = payload.get("collection")
    stored = config_store.stored()
    definition = source_sync.definition_for(
        str(collection or ""), {entry.id for entry in stored.sources.catalog}
    )
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection} wurde nicht entdeckt")
    data = stored.model_dump()
    data["sources"]["catalog"].append(definition.model_dump())
    new_config = AppConfig(**data)
    await _save_config(stored, new_config, move_existing=False)
    source_sync.forget_discovered(definition.collection or "")
    logger.info("Quelle %s (%s) in den Katalog übernommen", definition.id, definition.collection)
    return definition.model_dump()


@app.get("/api/logs")
async def get_logs(after: int = 0, limit: int = 500, format: str = "json"):
    entries = log_buffer.entries(after=after, limit=limit)
    if format == "text":
        lines = [f"{e['ts']} {e['level']:<7} {e['logger']}: {e['message']}" for e in entries]
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"entries": entries}


@app.get("/api/status")
async def get_status():
    return JSONResponse(status_store.all())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/regions/{region_name}/refresh")
async def refresh_region(region_name: str):
    config = config_store.get()
    _get_region_or_404(config, region_name)
    scheduler.trigger_now(region_name)
    logger.info("Manuelle Aktualisierung für %s angefordert", region_name)
    return {"status": "scheduled", "region": region_name}


@app.get("/api/regions/{region_name}/frames")
async def list_frames(region_name: str):
    """Historie: alle Frames im Puffer, neuester zuerst."""
    buffer = _buffer_for(region_name)
    return {
        "region": region_name,
        "frames": [
            {
                "index": i,
                "filename": frame.filename,
                "created_at": frame.created_at,
                "source": frame.source,
                "composite": frame.composite,
                "borders": frame.borders,
                "url": f"/regions/{region_name}/history/{frame.filename}",
                "jpeg_url": f"/regions/{region_name}/history/{Path(frame.filename).stem}.jpg",
            }
            for i, frame in enumerate(buffer.frames_newest_first())
        ],
    }


@app.get("/regions/{region_name}/history/{filename}")
async def history_frame(region_name: str, filename: str, w: int | None = None):
    """Frame per (stabilem) Dateinamen.

    ``<name>.png`` liefert das Original, ``<name>.jpg`` das gecachte JPEG in
    voller Größe (deutlich kleiner, schneller im Betrachter); ``?w=240``
    ein JPEG-Vorschaubild.
    """
    buffer = _buffer_for(region_name)
    as_jpeg = filename.endswith(".jpg")
    if as_jpeg:
        filename = filename[: -len(".jpg")] + ".png"
    frame = buffer.by_filename(filename)
    path = frame.path(buffer.region_dir) if frame else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Frame nicht (mehr) vorhanden")
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if w or as_jpeg:
        width = max(32, min(w, 1600)) if w else None
        data = await run_in_threadpool(media.jpeg, buffer, frame, width)
        return Response(content=data, media_type="image/jpeg", headers=headers)
    return Response(content=path.read_bytes(), media_type="image/png", headers=headers)


@app.get("/regions/{region_name}/latest.png")
async def latest_png(region_name: str):
    buffer = _buffer_for(region_name)
    frame = buffer.latest()
    if frame is None:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    return Response(
        content=frame.path(buffer.region_dir).read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/regions/{region_name}/latest.jpg")
async def latest_jpg(region_name: str):
    """Neuestes Einzelbild als gecachtes JPEG (schneller als das PNG)."""
    buffer = _buffer_for(region_name)
    frame = buffer.latest()
    if frame is None:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    try:
        data = await run_in_threadpool(media.jpeg, buffer, frame)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Frame nicht (mehr) vorhanden") from exc
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/regions/{region_name}/frames/{index}.png")
async def frame_png(region_name: str, index: int):
    buffer = _buffer_for(region_name)
    frame = buffer.get(index)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame-Index nicht vorhanden")
    return Response(content=frame.path(buffer.region_dir).read_bytes(), media_type="image/png")


@app.get("/regions/{region_name}/animation.gif")
async def animation_gif(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    payload = await run_in_threadpool(media.animation, buffer, "gif", fps)
    if payload is None:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    return Response(content=payload, media_type="image/gif")


@app.get("/regions/{region_name}/animation.mp4")
async def animation_mp4(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    try:
        payload = await run_in_threadpool(media.animation, buffer, "mp4", fps)
    except ImportError as exc:  # pragma: no cover - abhängig von optionaler Dependency
        raise HTTPException(
            status_code=501, detail="MP4-Export benötigt das Paket imageio-ffmpeg"
        ) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    return Response(content=payload, media_type="video/mp4")


@app.get("/regions/{region_name}/mjpeg")
async def mjpeg(region_name: str):
    _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    delay = 1.0 / fps if fps > 0 else 1.0

    async def _generate():
        while True:
            # Pro Durchlauf neu holen: neue Frames und ein zwischenzeitlich
            # gewechselter Speicherort werden so ohne Reconnect übernommen.
            try:
                buffer = _buffer_for(region_name)
            except HTTPException:
                return
            frames = buffer.frames_newest_first()
            if not frames:
                await asyncio.sleep(delay)
                continue
            for frame in reversed(frames):  # älteste zuerst abspielen, dann loopen
                try:
                    jpeg_bytes = await run_in_threadpool(media.jpeg, buffer, frame)
                except FileNotFoundError:
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n\r\n"
                    + jpeg_bytes + b"\r\n"
                )
                await asyncio.sleep(delay)

    return StreamingResponse(_generate(), media_type="multipart/x-mixed-replace; boundary=frame")

