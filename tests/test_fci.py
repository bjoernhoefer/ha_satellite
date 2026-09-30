"""Tests for the MTG FCI driver: chunk geometry, raw data archive, rendering.

``eumdac`` and the Satpy child process are mocked; the real Data Store is
never contacted.
"""

from __future__ import annotations

import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from ha_satellite import sources
from ha_satellite.buffer import BufferManager
from ha_satellite.config import (
    DEFAULT_FCI_COMPOSITE,
    AppConfig,
    ArchiveConfig,
    EumetsatCredentials,
    RegionConfig,
    default_config,
)
from ha_satellite.sources import MtgFciSource, NoNewData, RenderError, render_fci_slot
from ha_satellite.sources.fci_archive import (
    ArchiveError,
    FciArchive,
    _geos_y,
    chunk_of,
    chunks_for_region,
    format_chunks,
    render_cache_path,
    wanted_chunks,
)

CREDS = EumetsatCredentials(consumer_key="key", consumer_secret="secret")
SENSING = datetime(2026, 9, 26, 14, 20, tzinfo=timezone.utc)
COLLECTION = "EO:EUM:DAT:0662"


def _entry(chunk: int, kind: str = "BODY") -> str:
    return (
        f"W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-{kind}---NC4E_C_EUMT_"
        f"20260926142500_IDPFI_OPE_20260926141000_20260926142000_N_JLS_C_0086_{chunk:04d}.nc"
    )


class FakeFciProduct:
    def __init__(self, product_id: str, sensing_end: datetime, fail_on: int | None = None):
        self._id = product_id
        self.sensing_start = (sensing_end - timedelta(minutes=10)).replace(tzinfo=None)
        self.sensing_end = sensing_end.replace(tzinfo=None)
        self.entries = [_entry(c) for c in range(1, 41)] + [_entry(41, "TRAIL"), "manifest.xml"]
        self.opened: list[str] = []
        self.fail_on = fail_on

    def __str__(self) -> str:
        return self._id

    def open(self, entry: str):
        chunk = chunk_of(entry)
        if chunk == self.fail_on:
            raise OSError("connection aborted")
        self.opened.append(entry)
        return io.BytesIO(f"chunk{chunk}".encode())


def install_fake_eumdac(monkeypatch, products):
    module = types.ModuleType("eumdac")

    class Collection:
        def search(self, dtstart, dtend):
            return iter(products)

    class DataStore:
        def __init__(self, token):
            pass

        def get_collection(self, collection_id):
            return Collection()

    module.AccessToken = lambda credentials: "token"
    module.DataStore = DataStore
    monkeypatch.setitem(sys.modules, "eumdac", module)


def _region(name="wien", lat=48.2082, lon=16.3738, radius_km=300) -> RegionConfig:
    return RegionConfig(name=name, lat=lat, lon=lon, radius_km=radius_km, source="mtg_fci")


# -- Geometrie ------------------------------------------------------------------
def test_geos_projection_matches_pyproj():
    pyproj = pytest.importorskip("pyproj")
    proj = pyproj.Proj(proj="geos", h=35786400, lon_0=0, a=6378137, b=6356752.31414, sweep="y")
    for lat, lon in ((48.2, 16.4), (39.7, 3.0), (-30.0, 40.0), (60.0, -50.0)):
        assert _geos_y(lat, lon) == pytest.approx(proj(lon, lat)[1], abs=1.0)


def test_chunks_for_region_known_locations():
    # Verified against real data: Vienna renders completely from 36-37.
    assert chunks_for_region(_region()) == {36, 37}
    assert chunks_for_region(_region("mallorca", 39.6953, 3.0176)) == {34, 35}
    assert chunks_for_region(_region("oslo", 59.9, 10.7)) == {38, 39}
    assert chunks_for_region(_region("kapstadt", -33.9, 18.4)) == {7, 8, 9}
    # Outside the full disk (satellite over 0°).
    assert chunks_for_region(_region("tokio", 35.7, 139.7)) == set()


def test_wanted_chunks_is_range_plus_regions():
    config = default_config()
    config.archive = ArchiveConfig(chunk_min=38, chunk_max=40)
    config.regions.append(_region("kapstadt", -33.9, 18.4))
    assert wanted_chunks(config) == {7, 8, 9, 34, 35, 36, 37, 38, 39, 40}


def test_format_chunks():
    assert format_chunks({32, 33, 34, 36, 38, 39}) == "32–34, 36, 38–39"
    assert format_chunks([]) == "–"


def test_chunk_of_ignores_trail_and_other_files():
    assert chunk_of(_entry(36)) == 36
    assert chunk_of(_entry(41, "TRAIL")) is None
    assert chunk_of("manifest.xml") is None
    assert chunk_of(_entry(36) + ".part") is None


def test_archive_config_validation():
    with pytest.raises(ValueError, match="(?i)chunk"):
        ArchiveConfig(chunk_min=39, chunk_max=33)
    with pytest.raises(ValueError):
        ArchiveConfig(chunk_min=0)
    assert AppConfig().archive.retention_hours == 12


def test_region_names_must_not_collide_with_internal_dirs():
    with pytest.raises(ValueError, match="_"):
        RegionConfig(name="_archive", lat=0, lon=0, radius_km=10)
    with pytest.raises(ValueError):
        RegionConfig(name="a/b", lat=0, lon=0, radius_km=10)


# -- Archiv -------------------------------------------------------------------
def test_sync_downloads_only_wanted_chunks_once(tmp_path, monkeypatch):
    product = FakeFciProduct("FCI-A", SENSING)
    install_fake_eumdac(monkeypatch, [product])
    archive = FciArchive(tmp_path)

    slot = archive.sync(COLLECTION, CREDS, {36, 37, 41}, retention_hours=12)
    assert slot.name == "20260926T142000Z"
    assert slot.sensing_end == SENSING
    assert slot.chunks == [36, 37]  # 41 = TRAIL is never downloaded
    assert slot.chunk_files()[36].read_bytes() == b"chunk36"
    assert len(product.opened) == 2

    again = archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert again == slot and len(product.opened) == 2

    # Extended range: only the missing chunks are downloaded.
    archive.sync(COLLECTION, CREDS, {35, 36, 37}, retention_hours=12)
    assert len(product.opened) == 3
    assert [s.name for s in archive.slots(COLLECTION)] == [slot.name]
    assert archive.status()[COLLECTION]["last_error"] is None


def test_sync_prunes_slots_older_than_retention(tmp_path, monkeypatch):
    products: list[FakeFciProduct] = []
    install_fake_eumdac(monkeypatch, products)
    archive = FciArchive(tmp_path)
    for i in range(4):  # 0 h, 1 h, 2 h, 3 h
        products.insert(0, FakeFciProduct(f"FCI-{i}", SENSING + timedelta(hours=i)))
        archive.sync(COLLECTION, CREDS, {36}, retention_hours=2)
    names = [s.name for s in archive.slots(COLLECTION)]
    assert names == ["20260926T172000Z", "20260926T162000Z"]

    assert archive.prune(COLLECTION, 0) == 1
    assert [s.name for s in archive.slots(COLLECTION)] == ["20260926T172000Z"]


def test_sync_failure_cleans_up_and_is_reported(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING, fail_on=36)])
    archive = FciArchive(tmp_path)
    with pytest.raises(ArchiveError, match="aborted"):
        archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert archive.slots(COLLECTION) == []
    assert not list(tmp_path.rglob("*.part"))
    assert "aborted" in archive.status()[COLLECTION]["last_error"]


def test_sync_requires_credentials(tmp_path):
    with pytest.raises(ArchiveError, match="credentials"):
        FciArchive(tmp_path).sync(COLLECTION, EumetsatCredentials(), {36}, 12)


def test_slot_lookup_rejects_path_tricks(tmp_path):
    archive = FciArchive(tmp_path)
    assert archive.slot(COLLECTION, "../../etc") is None
    assert archive.slot(COLLECTION, "20260926T142000Z") is None


# -- Treiber ------------------------------------------------------------------
def _fci_config(tmp_path) -> AppConfig:
    config = default_config()
    config.storage.frames_dir = str(tmp_path / "frames")
    for entry in config.sources.catalog:
        entry.enabled = entry.id == "mtg_fci"
    for region in config.regions:
        region.source = "mtg_fci"
    config.eumetsat = CREDS
    return config


def test_mtg_fci_renders_only_region_chunks(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING)])
    requests = []

    def fake_render(request):
        requests.append(request)
        return b"PNG", SENSING.replace(tzinfo=None)

    monkeypatch.setattr("ha_satellite.sources.satpy_render.render_in_subprocess", fake_render)
    config = _fci_config(tmp_path)
    region = config.region("wien")
    region.composite = "natural_color_hrv_with_night_ir"  # SEVIRI name -> FCI counterpart

    frame = MtgFciSource().render(region, config)
    assert frame.png == b"PNG"
    assert frame.sensing_time == SENSING
    request = requests[0]
    assert request.reader == "fci_l1c_nc"
    assert request.composite == DEFAULT_FCI_COMPOSITE
    assert [chunk_of(f) for f in request.filenames] == [36, 37]

    # The Europe range was fully archived although Vienna needs only 2 chunks.
    slot = sources.fci_archive.get_archive(sources.archive_root(config)).slots("EO:EUM:DAT:0662")[0]
    assert slot.chunks == list(range(32, 41))
    assert slot.path.is_relative_to(tmp_path / "frames" / "_archive")

    with pytest.raises(NoNewData):
        MtgFciSource().render(region, config, last_sensing=SENSING)


def test_render_fci_slot_reports_missing_chunks(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING)])
    slot = FciArchive(tmp_path).sync(COLLECTION, CREDS, {36}, 12)
    with pytest.raises(RenderError, match="missing 37"):
        render_fci_slot(slot, _region(), DEFAULT_FCI_COMPOSITE)
    with pytest.raises(RenderError, match="outside"):
        render_fci_slot(slot, _region("tokio", 35.7, 139.7), DEFAULT_FCI_COMPOSITE)


def test_fci_is_no_placeholder_and_has_own_composites():
    from ha_satellite.config import PLACEHOLDER_DRIVERS, composites_for, default_composite_for

    assert "mtg_fci" not in PLACEHOLDER_DRIVERS
    assert DEFAULT_FCI_COMPOSITE in composites_for("mtg_fci")
    assert "natural_color_hrv" not in composites_for("mtg_fci")
    assert default_composite_for("msg_seviri") == "natural_color_hrv_with_night_ir"


def test_fci_composite_is_registered_in_satpy_config():
    pytest.importorskip("satpy")
    from ha_satellite.sources.satpy_render import SATPY_CONFIG_DIR

    text = (SATPY_CONFIG_DIR / "composites" / "visir.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text.replace("!!python/name:", ""))["composites"][DEFAULT_FCI_COMPOSITE]


# -- Speicherort-Wechsel --------------------------------------------------------
def test_relocate_moves_archive_as_whole(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    slot_dir = old / "_archive" / "EO_EUM_DAT_0662" / "20260926T142000Z"
    slot_dir.mkdir(parents=True)
    (slot_dir / "meta.json").write_text("{}")
    manager = BufferManager(old)
    manager.get("wien", 4, 100).add_frame(b"png")

    assert manager.relocate(new, move_existing=True) == 1
    assert (new / "_archive" / "EO_EUM_DAT_0662" / "20260926T142000Z" / "meta.json").exists()
    assert not (new / "_archive" / "_index.json").exists()
    assert not old.joinpath("_archive").exists()


# -- API ------------------------------------------------------------------------
def write_fci_slot(frames_dir, name="20260926T142000Z", chunks=range(32, 41), cached=None):
    """Creates an archived FCI slot; ``cached`` = {(region, composite): PNG}."""
    slot_dir = frames_dir / "_archive" / "EO_EUM_DAT_0662" / name
    slot_dir.mkdir(parents=True)
    end = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    (slot_dir / "meta.json").write_text(json.dumps({
        "product_id": "FCI-A", "sensing_start": end.isoformat(), "sensing_end": end.isoformat(),
    }))
    for c in chunks:
        (slot_dir / _entry(c)).write_bytes(b"x")
    if cached:
        config = default_config()
        slot = FciArchive(frames_dir / "_archive").slot(COLLECTION, name)
        for (region, composite), png in cached.items():
            path = render_cache_path(slot, config.region(region), composite)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
    return slot_dir


def test_archive_api_lists_slots_and_serves_cached_renders(live_server):
    frames_dir = live_server.data_dir / "frames"
    write_fci_slot(frames_dir, "20260926T141000Z", chunks=[36])  # Vienna incomplete
    write_fci_slot(frames_dir)

    info = httpx.get(f"{live_server.url}/api/archive").json()
    assert info["retention_hours"] == 12
    assert info["chunks_text"] == "32–40"
    assert info["collections"]["EO:EUM:DAT:0662"]["slots"] == 2

    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive").json()
    assert listing["composite"] == DEFAULT_FCI_COMPOSITE
    assert listing["chunks"] == [36, 37]
    assert [(s["name"], s["available"]) for s in listing["slots"]] == [
        ("20260926T142000Z", True), ("20260926T141000Z", False),
    ]

    # Bereits gerenderte Bilder kommen aus dem Cache (ohne Satpy).
    config = AppConfig(**yaml.safe_load(live_server.config_file.read_text()))
    archive = FciArchive(frames_dir / "_archive")
    slot = archive.slot(COLLECTION, "20260926T142000Z")
    cache = render_cache_path(slot, config.region("wien"), "cloudtop")
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"\x89PNG cached")
    response = httpx.get(f"{live_server.url}/regions/wien/archive/20260926T142000Z.png?composite=cloudtop")
    assert response.status_code == 200 and response.content == b"\x89PNG cached"
    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive?composite=cloudtop").json()
    assert listing["slots"][0]["cached"] is True

    assert httpx.get(f"{live_server.url}/regions/wien/archive/20260101T000000Z.png").status_code == 404
    assert httpx.get(f"{live_server.url}/regions/wien/archive/20260926T142000Z.png?composite=a/b").status_code in (400, 404)
    assert httpx.get(f"{live_server.url}/regions/wien/archive/20260926T142000Z.png?composite=x;y").status_code == 400
    # Vienna lacks chunk 37 in the older slot -> clear error instead of a crash.
    response = httpx.get(f"{live_server.url}/regions/wien/archive/20260926T141000Z.png", timeout=30)
    assert response.status_code == 502 and "missing 37" in response.json()["detail"]


def test_storage_api_sets_archive_settings_and_prunes(live_server):
    frames_dir = live_server.data_dir / "frames"
    write_fci_slot(frames_dir, "20260926T000000Z")
    write_fci_slot(frames_dir, "20260926T142000Z")
    response = httpx.post(f"{live_server.url}/api/storage", json={
        "archive_retention_hours": 1, "archive_chunk_min": 30, "archive_chunk_max": 40,
    })
    assert response.status_code == 200, response.text
    stored = yaml.safe_load(live_server.config_file.read_text())["archive"]
    assert stored == {"retention_hours": 1, "chunk_min": 30, "chunk_max": 40}
    remaining = sorted(p.name for p in (frames_dir / "_archive" / "EO_EUM_DAT_0662").iterdir())
    assert remaining == ["20260926T142000Z"]

    bad = httpx.post(f"{live_server.url}/api/storage", json={"archive_chunk_min": 40, "archive_chunk_max": 30})
    assert bad.status_code == 400
