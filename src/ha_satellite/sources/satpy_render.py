"""Satpy-Rendering eines regionalen Ausschnitts als PNG.

Läuft bewusst in einem eigenen Prozess (siehe ``render_in_subprocess``):
Satpy/dask geben Speicher nach einem Lauf nicht zuverlässig an das
Betriebssystem zurück, und ein OOM-Kill soll das Rendering treffen, nicht
den Webserver.
"""

from __future__ import annotations

import multiprocessing
import warnings
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

# Eigene Komposit-Definitionen (z. B. natural_color_hrv_with_night_ir).
SATPY_CONFIG_DIR = Path(__file__).resolve().parent.parent / "satpy_config"

# Randpixel um das berechnete Fenster, damit das Nearest-Neighbour-Resampling
# auch an den Kanten der Zielregion Nachbarn findet.
WINDOW_MARGIN_PX = 16
RENDER_TIMEOUT_SECONDS = 300


class SatpyRenderError(Exception):
    pass


@dataclass(frozen=True)
class RenderRequest:
    reader: str
    filenames: tuple[str, ...]
    composite: str
    lat: float
    lon: float
    radius_km: float
    width: int
    height: int
    label: str


def source_window(source_area, target_area, margin: int = WINDOW_MARGIN_PX):
    """Pixelfenster der Quell-Area, das die Zielregion vollständig abdeckt.

    Ersetzt pyresamples eigenes Vorab-Zuschneiden (``reduce_data``) bzw.
    ``Scene.crop``: Beide liefern für die gespiegelt gespeicherte
    SEVIRI-Rapid-Scan-Area ein viel zu kleines Fenster, wodurch z. B. Wien
    zu 99 % schwarz blieb (siehe HISTORY.md). ``get_array_indices_from_lonlat``
    rechnet dagegen korrekt.
    """
    import numpy as np

    lons, lats = target_area.get_lonlats()
    edge_lons = np.concatenate([lons[0], lons[-1], lons[:, 0], lons[:, -1]])
    edge_lats = np.concatenate([lats[0], lats[-1], lats[:, 0], lats[:, -1]])
    cols, rows = source_area.get_array_indices_from_lonlat(edge_lons, edge_lats)
    valid = ~(np.ma.getmaskarray(cols) | np.ma.getmaskarray(rows))
    cols, rows = np.ma.getdata(cols)[valid], np.ma.getdata(rows)[valid]
    if cols.size == 0:
        raise SatpyRenderError("Region liegt außerhalb des Satelliten-Sichtbereichs")
    height, width = source_area.shape
    return (
        slice(max(int(rows.min()) - margin, 0), min(int(rows.max()) + margin + 1, height)),
        slice(max(int(cols.min()) - margin, 0), min(int(cols.max()) + margin + 1, width)),
    )


def target_area(request: RenderRequest):
    from pyresample import create_area_def

    radius_m = request.radius_km * 1000
    return create_area_def(
        request.label,
        {"proj": "laea", "lat_0": request.lat, "lon_0": request.lon, "ellps": "WGS84"},
        width=request.width,
        height=request.height,
        area_extent=(-radius_m, -radius_m, radius_m, radius_m),
        units="m",
    )


def _crop_to(data, target):
    """Schneidet einen Datensatz auf das Fenster um die Zielregion zu.

    Das Fenster wird je Datensatz aus dessen eigener Area berechnet, damit
    Kanäle unterschiedlicher Auflösung (HRV ~1 km, übrige ~3 km) passen.
    """
    rows, cols = source_window(data.attrs["area"], target)
    cropped = data[..., rows, cols]
    cropped.attrs["area"] = data.attrs["area"][rows, cols]
    return cropped


def _annotate(image, text: str):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    left, top, right, bottom = draw.textbbox((0, 0), text)
    pad = 4
    box = (6, image.height - (bottom - top) - 2 * pad - 6, 6 + right - left + 2 * pad, image.height - 6)
    draw.rectangle(box, fill=(0, 0, 0))
    draw.text((box[0] + pad, box[1] + pad - top), text, fill=(255, 255, 255))
    return image


def render_png(request: RenderRequest) -> tuple[bytes, datetime]:
    """Rendert das Komposit für die Region und liefert (PNG, Aufnahmeende)."""
    warnings.filterwarnings("ignore")
    import dask

    # Zwei Threads: genug für den Pi 5, ohne Grafana auszubremsen.
    dask.config.set(scheduler="threads", num_workers=2)
    import satpy
    from satpy import Scene

    satpy.config.set(config_path=[str(SATPY_CONFIG_DIR)])
    # fill_disk: HRV liegt im Full Disk (0°) sonst als zwei gestapelte
    # Fenster vor (StackedAreaDefinition) - daran scheitern Zuschnitt und
    # sunz_corrected. Aufgefüllt wird lazy, der Zuschnitt lädt nur das Fenster.
    scene = Scene(
        reader=request.reader,
        filenames=list(request.filenames),
        reader_kwargs={"fill_disk": True} if request.reader == "seviri_l1b_native" else None,
    )
    available = set(scene.available_composite_names()) | {
        str(name) for name in scene.available_dataset_names()
    }
    if request.composite not in available:
        raise SatpyRenderError(
            f"Komposit '{request.composite}' ist für {request.reader} nicht verfügbar"
        )
    # Komposite aus Kanälen unterschiedlicher Auflösung (z. B. HRV + VIS)
    # erzeugt Satpy erst beim Resampling; bis dahin liegen nur die Kanäle vor.
    scene.load([request.composite], generate=False)
    area = target_area(request)
    for key in list(scene.keys()):
        scene._datasets[key] = _crop_to(scene[key], area)
    local = scene.resample(area, resampler="nearest", reduce_data=False)
    if request.composite not in local:
        raise SatpyRenderError(f"Komposit '{request.composite}' konnte nicht erzeugt werden")
    data = local[request.composite]

    from satpy.writers import get_enhanced_image

    pil_image = get_enhanced_image(data).pil_image().convert("RGB")
    sensing_end = data.attrs["end_time"]
    _annotate(pil_image, f"{request.label} · {sensing_end:%Y-%m-%d %H:%M} UTC · {request.composite}")
    out = BytesIO()
    pil_image.save(out, format="PNG", optimize=True)
    return out.getvalue(), sensing_end


def _worker(request: RenderRequest, connection) -> None:
    try:
        connection.send(("ok", render_png(request)))
    except Exception as exc:  # an den Elternprozess durchreichen
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def render_in_subprocess(request: RenderRequest, timeout: float = RENDER_TIMEOUT_SECONDS):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(request, child), daemon=True)
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise SatpyRenderError(f"Rendering hat das Zeitlimit von {timeout:.0f}s überschritten")
        status, payload = parent.recv()
    except EOFError as exc:
        raise SatpyRenderError(
            f"Render-Prozess wurde beendet (Exit-Code {process.exitcode}, evtl. Speicherlimit)"
        ) from exc
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()
    if status == "error":
        raise SatpyRenderError(payload)
    return payload


__all__ = [
    "RenderRequest",
    "SatpyRenderError",
    "render_in_subprocess",
    "render_png",
    "source_window",
    "target_area",
]
