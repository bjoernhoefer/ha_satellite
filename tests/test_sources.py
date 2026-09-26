"""Tests für die Phase-2-Quellen: Download-Cache, Dedup und Crop-Fenster.

``eumdac`` und der Satpy-Kindprozess werden gemockt; der Crop-Test nutzt
die echte Geometrie des SEVIRI-Rapid-Scans (gespiegelte Area, 3712x1392),
ohne dass ein ~100-MB-Produkt nötig ist.
"""

from __future__ import annotations

import io
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ha_satellite import sources
from ha_satellite.buffer import BufferManager
from ha_satellite.config import AppConfig, ConfigStore, EumetsatCredentials, default_config
from ha_satellite.scheduler import RenderScheduler
from ha_satellite.sources import (
    MsgSeviriSource,
    NoNewData,
    RenderedFrame,
    RenderError,
)
from ha_satellite.sources.eumetsat import DataStoreError, Product, ProductCache
from ha_satellite.status import StatusStore

CREDS = EumetsatCredentials(consumer_key="key", consumer_secret="secret")
SENSING = datetime(2026, 9, 26, 10, 40, tzinfo=timezone.utc)


# -- Fake eumdac -----------------------------------------------------------
class FakeProduct:
    def __init__(self, product_id: str, sensing_end: datetime, payload: bytes = b"NAT"):
        self._id = product_id
        self.sensing_end = sensing_end.replace(tzinfo=None)
        self.entries = [f"{product_id}.nat", "manifest.xml"]
        self.payload = payload
        self.open_calls = 0

    def __str__(self) -> str:
        return self._id

    def open(self, entry: str):
        assert entry.endswith(".nat")
        self.open_calls += 1
        return io.BytesIO(self.payload)


def install_fake_eumdac(monkeypatch, products, search_error: Exception | None = None):
    module = types.ModuleType("eumdac")
    calls = {"tokens": []}

    def access_token(credentials):
        calls["tokens"].append(credentials)
        return "token"

    class Collection:
        def search(self, dtstart, dtend):
            if search_error:
                raise search_error
            assert dtstart < dtend
            return iter(products)

    class DataStore:
        def __init__(self, token):
            assert token == "token"

        def get_collection(self, collection_id):
            calls["collection"] = collection_id
            return Collection()

    module.AccessToken = access_token
    module.DataStore = DataStore
    monkeypatch.setitem(sys.modules, "eumdac", module)
    return calls


# -- ProductCache ------------------------------------------------------------
def test_product_cache_downloads_once_and_keeps_only_latest(tmp_path, monkeypatch):
    first = FakeProduct("MSG4-A", SENSING, b"first")
    products = [first]
    calls = install_fake_eumdac(monkeypatch, products)
    cache = ProductCache(tmp_path)

    product = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")
    again = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")

    assert product == again
    assert product.path.read_bytes() == b"first"
    assert product.sensing_end == SENSING
    assert first.open_calls == 1
    assert calls["tokens"][0] == ("key", "secret")
    assert calls["collection"] == "EO:EUM:DAT:MSG:MSG15-RSS"

    newer = FakeProduct("MSG4-B", SENSING + timedelta(minutes=5), b"second")
    products.insert(0, newer)
    latest = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")
    assert latest.product_id == "MSG4-B"
    assert [p.name for p in latest.path.parent.iterdir()] == [latest.path.name]


def test_product_cache_requires_credentials(tmp_path):
    with pytest.raises(DataStoreError, match="Zugangsdaten"):
        ProductCache(tmp_path).latest("c", EumetsatCredentials(), ".nat")


def test_product_cache_wraps_search_errors(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [], search_error=RuntimeError("401 Unauthorized"))
    with pytest.raises(DataStoreError, match="401"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")


def test_product_cache_reports_empty_search(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [])
    with pytest.raises(DataStoreError, match="Kein Produkt"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")


def test_product_cache_removes_partial_download(tmp_path, monkeypatch):
    broken = FakeProduct("MSG4-X", SENSING)

    def failing_open(entry):
        raise TimeoutError("read timed out")

    broken.open = failing_open
    install_fake_eumdac(monkeypatch, [broken])
    with pytest.raises(DataStoreError, match="timed out"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")
    assert not any(tmp_path.rglob("*.part"))


# -- MsgSeviriSource -----------------------------------------------------------
class StubCache:
    def __init__(self, product: Product | None = None, error: Exception | None = None):
        self.product = product
        self.error = error

    def latest(self, collection_id, credentials, suffix):
        if self.error:
            raise self.error
        return self.product


@pytest.fixture
def config() -> AppConfig:
    data = default_config().model_dump()
    data["eumetsat"] = CREDS.model_dump()
    return AppConfig(**data)


def test_msg_seviri_renders_new_product(tmp_path, monkeypatch, config):
    product = Product("MSG4-A", SENSING, tmp_path / "a.nat")
    monkeypatch.setattr(sources, "_product_cache", lambda: StubCache(product))
    seen = {}

    def fake_render(request):
        seen["request"] = request
        return b"PNG", SENSING.replace(tzinfo=None)

    monkeypatch.setattr("ha_satellite.sources.satpy_render.render_in_subprocess", fake_render)

    frame = MsgSeviriSource().render(config.regions[0], config, SENSING - timedelta(minutes=5))

    assert frame == RenderedFrame(b"PNG", SENSING)
    assert seen["request"].filenames == (str(product.path),)
    assert seen["request"].composite == config.regions[0].composite
    assert seen["request"].reader == "seviri_l1b_native"


def test_msg_seviri_skips_already_rendered_product(tmp_path, monkeypatch, config):
    product = Product("MSG4-A", SENSING, tmp_path / "a.nat")
    monkeypatch.setattr(sources, "_product_cache", lambda: StubCache(product))
    with pytest.raises(NoNewData):
        MsgSeviriSource().render(config.regions[0], config, SENSING)


def test_msg_seviri_maps_errors_to_render_error(tmp_path, monkeypatch, config):
    monkeypatch.setattr(
        sources, "_product_cache", lambda: StubCache(error=DataStoreError("kaputt"))
    )
    with pytest.raises(RenderError, match="kaputt"):
        MsgSeviriSource().render(config.regions[0], config, None)


# -- Scheduler -------------------------------------------------------------------
class SlowSource:
    """Rendert langsam und prüft, dass nie zwei Läufe parallel sind."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.rendered: list[str] = []
        self.guard = threading.Lock()

    def render(self, region, config, last_sensing=None):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.2)
        with self.guard:
            self.active -= 1
            self.rendered.append(region.name)
        if last_sensing is not None and last_sensing >= SENSING:
            raise NoNewData("gleiches Produkt")
        return RenderedFrame(b"PNG-" + region.name.encode(), SENSING)


def test_concurrent_regions_wait_for_lock_instead_of_skipping(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    status = StatusStore()
    scheduler = RenderScheduler(store, buffers, status)
    source = SlowSource()
    monkeypatch.setattr("ha_satellite.scheduler.get_source", lambda name: source)

    threads = [
        threading.Thread(target=scheduler._run_region, args=(name,))
        for name in ("wien", "mallorca")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(source.rendered) == ["mallorca", "wien"]
    assert source.max_active == 1
    for name in ("wien", "mallorca"):
        buffer = buffers.get(name, 4, 500)
        assert len(buffer) == 1
        assert datetime.fromisoformat(buffer.latest().created_at) == SENSING
        assert status.get(name).last_error is None

    # Zweiter Lauf: gleiche Aufnahme -> kein neuer Frame, aber auch kein Fehler.
    scheduler._run_region("wien")
    assert len(buffers.get("wien", 4, 500)) == 1
    assert status.get("wien").last_error is None


# -- Crop-Fenster ------------------------------------------------------------------
pyresample = pytest.importorskip("pyresample")

RSS_PROJ = (
    "+proj=geos +lon_0=9.5 +h=35785831 +x_0=0 +y_0=0 +a=6378169 "
    "+rf=295.488065897014 +units=m +no_defs"
)
# Wie im Produkt: x-Extent absteigend (gespiegelt), nur der nördliche Streifen.
RSS_EXTENT = (5567248.0, 5570248.5, -5570248.5, 1393687.2)


@pytest.fixture(scope="module")
def rss_area():
    from pyresample.geometry import AreaDefinition

    return AreaDefinition("rss", "rss", "rss", RSS_PROJ, 3712, 1392, RSS_EXTENT)


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [("wien", 48.2082, 16.3738), ("mallorca", 39.6953, 3.0176), ("oslo", 59.91, 10.75)],
)
def test_source_window_covers_whole_target_region(rss_area, name, lat, lon):
    from ha_satellite.sources.satpy_render import RenderRequest, source_window, target_area

    request = RenderRequest("r", ("f",), "c", lat, lon, 300, 200, 200, name)
    area = target_area(request)
    rows, cols = source_window(rss_area, area)

    lons, lats = area.get_lonlats()
    all_cols, all_rows = rss_area.get_array_indices_from_lonlat(lons[::10, ::10], lats[::10, ::10])
    assert not np.ma.is_masked(all_cols)
    assert rows.start <= int(all_rows.min()) and int(all_rows.max()) < rows.stop
    assert cols.start <= int(all_cols.min()) and int(all_cols.max()) < cols.stop
    # 600 km bei ~3-5 km Pixeln: Fenster muss deutlich über 100 Pixel groß sein
    # (pyresamples reduce_data lieferte für Wien nur 26x4).
    assert rows.stop - rows.start > 100
    assert cols.stop - cols.start > 100


def test_source_window_rejects_region_outside_disk(rss_area):
    from ha_satellite.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        source_window,
        target_area,
    )

    request = RenderRequest("r", ("f",), "c", 35.0, 139.0, 300, 50, 50, "tokio")
    with pytest.raises(SatpyRenderError, match="außerhalb"):
        source_window(rss_area, target_area(request))


# -- HRV-Schärfung -----------------------------------------------------------------
HRV_EXTENT = (5566247.7, 5571249.0, -5571249.0, 1392686.9)


def test_crop_uses_own_window_per_resolution(rss_area):
    import xarray as xr
    from pyresample.geometry import AreaDefinition

    from ha_satellite.sources.satpy_render import RenderRequest, _crop_to, target_area

    hrv_area = AreaDefinition("hrv", "hrv", "hrv", RSS_PROJ, 3712 * 3, 1392 * 3, HRV_EXTENT)
    target = target_area(RenderRequest("r", ("f",), "c", 48.2, 16.37, 300, 200, 200, "wien"))
    vis = xr.DataArray(np.zeros(rss_area.shape), dims=("y", "x"), attrs={"area": rss_area})
    hrv = xr.DataArray(np.zeros(hrv_area.shape), dims=("y", "x"), attrs={"area": hrv_area})

    vis_crop, hrv_crop = _crop_to(vis, target), _crop_to(hrv, target)

    assert vis_crop.attrs["area"].shape == vis_crop.shape
    assert hrv_crop.attrs["area"].shape == hrv_crop.shape
    # gleiches Gebiet, dreifache Auflösung (± Rand)
    assert abs(hrv_crop.shape[0] - 3 * vis_crop.shape[0]) < 3 * 2 * 16
    assert abs(hrv_crop.shape[1] - 3 * vis_crop.shape[1]) < 3 * 2 * 16


def _band(values, name):
    import xarray as xr

    return xr.DataArray(
        np.array(values, dtype=float), dims=("y", "x"), attrs={"name": name, "units": "%"}
    )


def test_hrv_sharpening_scales_brightness_and_keeps_hue():
    pytest.importorskip("satpy")
    from ha_satellite.sources.hrv_composite import MAX_RATIO, HrvLuminanceSharpenedRGB

    red, green, blue = _band([[10, 10]], "IR_016"), _band([[20, 20]], "VIS008"), _band([[40, 40]], "VIS006")
    hrv = _band([[60, 300]], "HRV")  # Faktor 2 bzw. 10 (-> gekappt)

    result = HrvLuminanceSharpenedRGB("natural_color_hrv")((red, green, blue), optional_datasets=(hrv,))

    assert result.sel(bands="R").values.tolist() == [[20, 10 * MAX_RATIO]]
    assert result.sel(bands="G").values.tolist() == [[40, 20 * MAX_RATIO]]
    assert result.sel(bands="B").values.tolist() == [[80, 40 * MAX_RATIO]]


def test_hrv_sharpening_ignores_invalid_hrv_and_waits_for_resampling():
    pytest.importorskip("satpy")
    from satpy.composites.core import IncompatibleAreas

    from ha_satellite.sources.hrv_composite import HrvLuminanceSharpenedRGB

    compositor = HrvLuminanceSharpenedRGB("natural_color_hrv")
    rgb = (_band([[10]], "IR_016"), _band([[20]], "VIS008"), _band([[40]], "VIS006"))
    result = compositor(rgb, optional_datasets=(_band([[np.nan]], "HRV"),))
    assert result.sel(bands="G").values.tolist() == [[20]]

    with pytest.raises(IncompatibleAreas):
        compositor(rgb, optional_datasets=(_band([[1, 2, 3]], "HRV"),))


def test_default_composite_is_defined_for_seviri():
    satpy = pytest.importorskip("satpy")
    from satpy.composites.config_loader import load_compositor_configs_for_sensors

    from ha_satellite.config import DEFAULT_COMPOSITE
    from ha_satellite.sources.satpy_render import SATPY_CONFIG_DIR

    with satpy.config.set(config_path=[str(SATPY_CONFIG_DIR)]):
        compositors, _ = load_compositor_configs_for_sensors(["seviri"])
    names = {key["name"] for key in compositors["seviri"]}
    assert {DEFAULT_COMPOSITE, "natural_color_hrv"} <= names


def test_source_window_ignores_points_with_only_one_valid_index():
    from ha_satellite.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        source_window,
        target_area,
    )

    class HalfValidArea:
        """Wie das südliche HRV-Fenster im Full Disk: Spalten gültig, Zeilen nicht."""

        shape = (100, 100)

        def get_array_indices_from_lonlat(self, lons, lats):
            cols = np.ma.masked_array(np.full(lons.shape, 50), mask=False)
            rows = np.ma.masked_array(np.zeros(lons.shape, dtype=int), mask=True)
            return cols, rows

    request = RenderRequest("r", ("f",), "c", 48.2, 16.37, 300, 20, 20, "wien")
    with pytest.raises(SatpyRenderError, match="außerhalb"):
        source_window(HalfValidArea(), target_area(request))


# -- Quellen-/Kompositwechsel ----------------------------------------------------
class OriginSource:
    """Liefert je Quelle eine feste Aufnahmezeit (0° hinkt Rapid Scan hinterher)."""

    SENSING_BY_SOURCE = {"msg_seviri": SENSING, "msg_seviri_0deg": SENSING - timedelta(minutes=13)}

    def __init__(self):
        self.calls: list[tuple[str, str, datetime | None]] = []

    def render(self, region, config, last_sensing=None):
        sensing = self.SENSING_BY_SOURCE[region.source]
        self.calls.append((region.source, region.composite, last_sensing))
        if last_sensing is not None and sensing <= last_sensing:
            raise NoNewData("bereits gerendert")
        return RenderedFrame(f"{region.source}/{region.composite}".encode(), sensing)


def _switch(store: ConfigStore, **changes) -> None:
    config = store.get()
    regions = [r.model_copy(update=changes) if r.name == "wien" else r for r in config.regions]
    store.update(config.model_copy(update={"regions": regions}))


def test_switching_source_or_composite_renders_even_older_product(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    scheduler = RenderScheduler(store, buffers, StatusStore())
    source = OriginSource()
    monkeypatch.setattr("ha_satellite.scheduler.get_source", lambda name: source)
    buffer = lambda: buffers.get("wien", 10, 500)  # noqa: E731

    scheduler._run_region("wien")
    assert len(buffer()) == 1

    # Wechsel auf 0°: Aufnahme ist älter als der Rapid-Scan-Frame, wird aber
    # trotzdem gerendert und ist danach der neueste Frame.
    _switch(store, source="msg_seviri_0deg")
    scheduler._run_region("wien")
    assert len(buffer()) == 2
    assert buffer().latest().source == "msg_seviri_0deg"
    assert buffer().get(0).path(buffer().region_dir).read_bytes().startswith(b"msg_seviri_0deg/")

    # Gleiche Quelle erneut: kein doppelter Frame.
    scheduler._run_region("wien")
    assert len(buffer()) == 2

    # Nur das Komposit ändern: gleiche Aufnahme, trotzdem neues Bild.
    _switch(store, composite="natural_color_hrv")
    scheduler._run_region("wien")
    assert len(buffer()) == 3
    assert buffer().latest().composite == "natural_color_hrv"

    # Zurück auf Rapid Scan: wieder sofort ein Bild.
    _switch(store, source="msg_seviri")
    scheduler._run_region("wien")
    assert buffer().latest().source == "msg_seviri"
    assert len(buffer()) == 4


def test_reload_keeps_schedule_of_unchanged_regions(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    scheduler._scheduler.start(paused=True)
    try:
        scheduler.reload()
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        for name in ("wien", "mallorca"):
            scheduler._scheduler.get_job(f"render-{name}").modify(next_run_time=later)

        _switch(store, composite="natural_color_hrv")
        scheduler.reload()

        wien = scheduler._scheduler.get_job("render-wien").next_run_time
        mallorca = scheduler._scheduler.get_job("render-mallorca").next_run_time
        assert wien < later - timedelta(minutes=5)  # geändert -> sofort
        assert mallorca == later  # unverändert -> Takt bleibt
    finally:
        scheduler._scheduler.shutdown(wait=False)


def test_render_subprocess_retries_once_after_crash(monkeypatch):
    import signal

    from ha_satellite.sources import satpy_render

    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise satpy_render.RenderProcessCrashed("Render-Prozess durch SIGSEGV beendet")
        return b"png", "t"

    monkeypatch.setattr(satpy_render, "_run_once", flaky)
    assert satpy_render.render_in_subprocess(object()) == (b"png", "t")
    assert len(calls) == 2

    def always_crash(request, timeout):
        raise satpy_render.RenderProcessCrashed("kaputt")

    monkeypatch.setattr(satpy_render, "_run_once", always_crash)
    with pytest.raises(satpy_render.SatpyRenderError, match="kaputt"):
        satpy_render.render_in_subprocess(object())

    def timeout(request, timeout):
        calls.append(1)
        raise satpy_render.SatpyRenderError("Zeitlimit")

    calls.clear()
    monkeypatch.setattr(satpy_render, "_run_once", timeout)
    with pytest.raises(satpy_render.SatpyRenderError, match="Zeitlimit"):
        satpy_render.render_in_subprocess(object())
    assert len(calls) == 1  # kein Retry bei Zeitüberschreitung

    assert "SIGKILL" in satpy_render._describe_exit(-signal.SIGKILL)
    assert "Speicherlimit" in satpy_render._describe_exit(-signal.SIGKILL)
    assert "SIGSEGV" in satpy_render._describe_exit(-signal.SIGSEGV)
    assert "Exit-Code 1" in satpy_render._describe_exit(1)
