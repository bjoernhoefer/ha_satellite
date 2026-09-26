"""FastAPI-Anwendung: Konfigurations-UI, REST-API und Bild-Endpunkte."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from PIL import Image

from ha_satellite.buffer import BufferManager
from ha_satellite.config import AppConfig, ConfigStore, EumetsatCredentials, env_overrides
from ha_satellite.scheduler import RenderScheduler
from ha_satellite.status import StatusStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HA_SATELLITE_DATA_DIR", "/data"))
FRAMES_DIR = DATA_DIR / "frames"

config_store = ConfigStore()
buffer_manager = BufferManager(FRAMES_DIR)
status_store = StatusStore()
scheduler = RenderScheduler(config_store, buffer_manager, status_store)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
    max_frames = region.effective_max_frames(config.sources.poll_interval_minutes)
    return buffer_manager.get(region.name, max_frames, config.history.max_storage_mb)


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
        },
    )


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
    config_store.update(new_config)
    scheduler.reload()
    return JSONResponse(config_store.get().masked().model_dump())


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
    return {"status": "scheduled", "region": region_name}


@app.get("/regions/{region_name}/latest.png")
async def latest_png(region_name: str):
    buffer = _buffer_for(region_name)
    frame = buffer.latest()
    if frame is None:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    return Response(content=frame.path(buffer.region_dir).read_bytes(), media_type="image/png")


@app.get("/regions/{region_name}/frames/{index}.png")
async def frame_png(region_name: str, index: int):
    buffer = _buffer_for(region_name)
    frame = buffer.get(index)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame-Index nicht vorhanden")
    return Response(content=frame.path(buffer.region_dir).read_bytes(), media_type="image/png")


def _load_images(buffer) -> list[Image.Image]:
    frames = list(reversed(buffer.frames_newest_first()))  # älteste zuerst
    images = []
    for frame in frames:
        with Image.open(frame.path(buffer.region_dir)) as img:
            images.append(img.convert("RGB"))
    return images


@app.get("/regions/{region_name}/animation.gif")
async def animation_gif(region_name: str):
    buffer = _buffer_for(region_name)
    images = _load_images(buffer)
    if not images:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    fps = config_store.get().server.mjpeg_fps
    duration_ms = max(int(1000 / fps), 50)
    out = io.BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
    )
    return Response(content=out.getvalue(), media_type="image/gif")


@app.get("/regions/{region_name}/animation.mp4")
async def animation_mp4(region_name: str):
    buffer = _buffer_for(region_name)
    images = _load_images(buffer)
    if not images:
        raise HTTPException(status_code=404, detail="Noch keine Frames vorhanden")
    try:
        import imageio.v2 as imageio
    except ImportError as exc:  # pragma: no cover - abhängig von optionaler Dependency
        raise HTTPException(
            status_code=501, detail="MP4-Export benötigt das Paket imageio-ffmpeg"
        ) from exc

    import numpy as np

    fps = config_store.get().server.mjpeg_fps
    # Das FFMPEG-Plugin von imageio schreibt nur in echte Dateien, nicht in
    # BytesIO - daher der Umweg über eine temporäre Datei.
    def _encode() -> bytes:
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
                    writer.append_data(np.asarray(image.convert("RGB")))
            return target.read_bytes()

    payload = await run_in_threadpool(_encode)
    return Response(content=payload, media_type="video/mp4")


@app.get("/regions/{region_name}/mjpeg")
async def mjpeg(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    delay = 1.0 / fps if fps > 0 else 1.0

    async def _generate():
        while True:
            frames = buffer.frames_newest_first()
            if not frames:
                await asyncio.sleep(delay)
                continue
            for frame in reversed(frames):  # älteste zuerst abspielen, dann loopen
                frame_bytes = await run_in_threadpool(
                    frame.path(buffer.region_dir).read_bytes
                )
                jpeg_bytes = await run_in_threadpool(_png_to_jpeg, frame_bytes)
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n\r\n"
                    + jpeg_bytes + b"\r\n"
                )
                await asyncio.sleep(delay)

    return StreamingResponse(_generate(), media_type="multipart/x-mixed-replace; boundary=frame")


def _png_to_jpeg(png_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as img:
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG")
        return out.getvalue()
