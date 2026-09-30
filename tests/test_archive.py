"""Archive of pre-rendered images ("download all sources")."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
import yaml
from PIL import Image

from ha_satellite.archive import RenderArchive, region_signature, slot_name, slot_time
from ha_satellite.config import RegionConfig

NOW = datetime(2026, 9, 26, 14, 20, tzinfo=timezone.utc)


def png(color: str = "red", size: int = 8) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, format="PNG")
    return out.getvalue()


def test_slot_names_round_trip():
    assert slot_name(NOW) == "20260926T142000Z"
    assert slot_time("20260926T142000Z") == NOW
    assert slot_time("not-a-slot") is None
    naive = NOW.replace(tzinfo=None)
    assert slot_name(naive) == "20260926T142000Z"


def test_store_writes_png_and_jpeg_and_is_listed(tmp_path):
    archive = RenderArchive(tmp_path)
    image = archive.store("wien", "msg_seviri", "cloudtop", NOW, png())
    assert image is not None
    assert image.path.read_bytes() == png()
    assert image.jpeg_path.exists()
    assert Image.open(image.jpeg_path).format == "JPEG"

    assert archive.has("wien", "msg_seviri", "cloudtop", NOW) is True
    assert archive.has("wien", "msg_seviri", "cloudtop", NOW + timedelta(minutes=5)) is False
    listed = archive.images("wien", "msg_seviri", "cloudtop")
    assert [i.slot for i in listed] == ["20260926T142000Z"]
    assert listed[0].as_dict("wien")["url"] == (
        "/regions/wien/archive/msg_seviri/cloudtop/20260926T142000Z.png"
    )
    assert archive.count() == 1 and archive.used_bytes() > 0


def test_images_are_sorted_newest_first(tmp_path):
    archive = RenderArchive(tmp_path)
    for minutes in (0, 10, 5):
        archive.store("wien", "dummy", "natural_color", NOW + timedelta(minutes=minutes), png())
    assert [i.slot for i in archive.images("wien", "dummy", "natural_color")] == [
        "20260926T143000Z", "20260926T142500Z", "20260926T142000Z",
    ]


def test_combinations_and_regions(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("wien", "dummy", "airmass", NOW, png())
    archive.store("wien", "mtg_fci", "cloudtop", NOW, png())
    archive.store("graz", "dummy", "cloudtop", NOW, png())
    assert archive.regions() == ["graz", "wien"]
    assert archive.combinations("wien") == {
        "dummy": ["airmass", "cloudtop"], "mtg_fci": ["cloudtop"],
    }
    assert archive.combinations("unknown") == {}


def test_invalid_names_are_rejected(tmp_path):
    archive = RenderArchive(tmp_path)
    assert archive.store("../etc", "dummy", "cloudtop", NOW, png()) is None
    assert archive.store("wien", "a/b", "cloudtop", NOW, png()) is None
    assert archive.path_for("wien", "dummy", "cloudtop", "nope") is None
    assert archive.path_for("wien", "dummy", "cloudtop", "20260926T142000Z", "exe") is None
    assert list(tmp_path.rglob("*.png")) == []


def test_prune_by_age_and_storage_limit(tmp_path):
    archive = RenderArchive(tmp_path)
    now = datetime.now(timezone.utc)
    for hours in (30, 20, 1):
        archive.store("wien", "dummy", "cloudtop", now - timedelta(hours=hours), png())
    assert archive.prune(retention_hours=24, max_storage_mb=1000) == 1
    assert len(archive.images("wien", "dummy", "cloudtop")) == 2

    # Storage limit removes the oldest first.
    assert archive.prune(retention_hours=0, max_storage_mb=0.0005) >= 1
    remaining = archive.images("wien", "dummy", "cloudtop")
    assert len(remaining) < 2
    # Retention 0 means "no age limit".
    assert archive.prune(retention_hours=0, max_storage_mb=1000) == 0


def test_forget_removes_regions_that_no_longer_exist(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("graz", "dummy", "cloudtop", NOW, png())
    assert archive.forget({"wien"}) == 1
    assert archive.regions() == ["wien"]


def test_region_signature_change_clears_the_archive(tmp_path):
    archive = RenderArchive(tmp_path)
    region = RegionConfig(name="wien", lat=48.2, lon=16.4, radius_km=100)
    archive.sync_region("wien", region_signature(region))
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    # Same geometry -> images are kept.
    archive.sync_region("wien", region_signature(region))
    assert archive.count() == 1
    # Different cut-out -> archived images no longer match and are dropped.
    moved = region.model_copy(update={"radius_km": 300})
    archive.sync_region("wien", region_signature(moved))
    assert archive.count() == 0


def test_summary_reports_images_per_region(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("wien", "dummy", "cloudtop", NOW + timedelta(minutes=5), png())
    summary = archive.summary()
    assert summary["images"] == 2
    assert summary["regions"]["wien"]["images"] == 2
    assert summary["regions"]["wien"]["combinations"] == 1
    assert summary["regions"]["wien"]["newest"].startswith("2026-09-26T14:25")


# --------------------------------------------------------------------------
# API


def archive_of(live_server) -> RenderArchive:
    return RenderArchive(live_server.data_dir / "frames" / "_renders")


def test_archive_api_lists_sources_composites_and_images(live_server):
    archive = archive_of(live_server)
    archive.store("wien", "dummy", "cloudtop", NOW, png("blue"))
    archive.store("wien", "dummy", "airmass", NOW, png("green"))

    data = httpx.get(f"{live_server.url}/api/regions/wien/archive").json()
    assert data["region"] == "wien"
    assert {s["id"] for s in data["sources"]} >= {"dummy"}
    assert "cloudtop" in data["composites"]  # id -> label
    assert data["images"], data
    # Backwards compatible alias for older clients.
    assert data["slots"] == data["images"]

    listing = httpx.get(
        f"{live_server.url}/api/regions/wien/archive?source=dummy&composite=airmass"
    ).json()
    assert listing["composite"] == "airmass"
    assert [i["name"] for i in listing["images"]] == ["20260926T142000Z"]

    unknown = httpx.get(f"{live_server.url}/api/regions/nope/archive")
    assert unknown.status_code == 404


def test_archive_images_are_served_as_png_and_jpeg(live_server):
    archive_of(live_server).store("wien", "dummy", "cloudtop", NOW, png("blue"))
    base = f"{live_server.url}/regions/wien/archive/dummy/cloudtop/20260926T142000Z"
    image = httpx.get(f"{base}.png")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == png("blue")
    jpeg = httpx.get(f"{base}.jpg")
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    assert httpx.get(f"{base}.gif").status_code in (400, 404)
    assert httpx.get(
        f"{live_server.url}/regions/wien/archive/dummy/cloudtop/20200101T000000Z.png"
    ).status_code == 404


def test_render_all_setting_and_manual_run(live_server):
    response = httpx.post(f"{live_server.url}/api/storage", json={"archive_render_all": True})
    assert response.status_code == 200, response.text
    stored = yaml.safe_load(live_server.config_file.read_text())["archive"]
    assert stored["render_all"] is True

    started = httpx.post(f"{live_server.url}/api/archive/render", json={})
    assert started.status_code == 200, started.text
    assert started.json()["sources"] >= 1

    # The dummy source renders quickly - the archive fills up on its own.
    deadline = 30
    for _ in range(deadline * 2):
        if archive_of(live_server).count():
            break
        import time

        time.sleep(0.5)
    archive = archive_of(live_server)
    assert archive.count() > 0, "nothing was archived"
    combos = archive.combinations("wien")
    assert "dummy" in combos
    # Every image type of the driver is archived, not only the configured one.
    assert len(combos["dummy"]) > 1
    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive?source=dummy").json()
    assert listing["images"] and listing["images"][0]["cached"] is True
    image = httpx.get(live_server.url + listing["images"][0]["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
