"""FastAPI-Anwendung: Konfigurations-UI, REST-API und Bild-Endpunkte."""

from __future__ import annotations

import io
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from PIL import Image

from ha_satellite.buffer import BufferManager
from ha_satellite.config import AppConfig, ConfigStore
from ha_satellite.scheduler import RenderScheduler
from ha_satellite.status import StatusStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/data")
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
        {"config": config, "status": status_store.all()},
    )


@app.get("/api/config")
async def get_config():
    return JSONResponse(config_store.get().masked().model_dump())


@app.post("/api/config")
async def post_config(payload: dict):
    current = config_store.get()
    merged = current.model_dump()
    merged.update(payload)
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

    fps = config_store.get().server.mjpeg_fps
    out = io.BytesIO()
    with imageio.get_writer(out, format="ffmpeg", mode="I", fps=fps, output_params=["-f", "mp4"]) as writer:
        for image in images:
            import numpy as np

            writer.append_data(np.asarray(image))
    return Response(content=out.getvalue(), media_type="video/mp4")


@app.get("/regions/{region_name}/mjpeg")
async def mjpeg(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    delay = 1.0 / fps if fps > 0 else 1.0

    def _generate():
        while True:
            frames = buffer.frames_newest_first()
            if not frames:
                time.sleep(delay)
                continue
            for frame in reversed(frames):  # älteste zuerst abspielen, dann loopen
                jpeg_bytes = _png_to_jpeg(frame.path(buffer.region_dir).read_bytes())
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n\r\n"
                    + jpeg_bytes + b"\r\n"
                )
                time.sleep(delay)

    return StreamingResponse(_generate(), media_type="multipart/x-mixed-replace; boundary=frame")


def _png_to_jpeg(png_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as img:
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG")
        return out.getvalue()
